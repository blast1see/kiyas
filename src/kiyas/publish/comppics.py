"""Uploading a comparison to a Comps instance -- comp.pics or a self-hosted one.

Unlike slow.pics, this API is documented. The server publishes an OpenAPI 3.1
document at ``/openapi.json``, serves Swagger UI at ``/api/docs``, and the
software behind it is open source, so the shapes below were read off the spec
and the server's own source rather than off a client. The flow is:

1. ``POST /api/v1/comparisons`` with the grid size and the metadata. The answer
   carries the comparison id, and -- on instances new enough to have it -- an
   ``edit_token`` authorising the writes that follow.
2. ``POST /api/v1/comparison/{id}/image`` once per cell, carrying the file plus
   its ``row`` and ``column``.
3. The comparison is then at ``{base}/compare/{id}``.

Two things differ from slow.pics in ways worth stating outright, because both
are places where carrying the other backend's habits over would be wrong:

**There is no transpose here.** slow.pics answers with ``images[frame][source]``
while kiyas holds ``sources[source][frame]``, and getting that backwards
uploads every picture into the wrong cell without erroring. This server takes
``row`` and ``column`` as separate fields, so the mapping is direct: ``row`` is
the frame, ``column`` is the source. The temptation is to transpose out of
habit; doing so here is the bug, not the fix.

**Every image has its own address**: ``{base}/uploads/{id}/{filename}``, built
from the ``filename`` each upload returns. slow.pics hands back nothing
per-image, which is why ``bbcode``'s markup formats degrade to a bare
collection link there. Here they can do the job they were written for, which is
what ``UploadResult.image_urls`` is for.
"""

from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .. import __version__
from . import transport
from .manifest import Comparison
from .result import UploadError, UploadResult

BASE_URL = "https://comp.pics"

#: Read when no key is passed. An environment variable rather than a config
#: file on purpose: kiyas has no credential store, and adding one to hold a
#: single optional token would mean writing somebody's secret to disk for a
#: feature that works without it.
API_KEY_ENV = "KIYAS_COMPPICS_API_KEY"

#: Read when no base URL is passed, so a self-hosted instance can be the
#: default for a whole shell rather than a flag repeated on every command.
#: ``run --publish`` has no place to put a URL, and this is how it reaches one.
HOST_URL_ENV = "KIYAS_COMPPICS_URL"

#: The server takes these and only these; anything else comes back 422. It is
#: an enum in the API (``ExpirationDays``), not a free integer, which is why a
#: day count from the command line has to be snapped onto it rather than passed
#: through.
EXPIRATION_DAYS = (1, 7, 30, 90)

#: How the clock is read: from when the comparison was made, or from the last
#: time somebody looked at it.
EXPIRATION_TYPES = ("from_creation", "from_last_access")

#: The server clamps ``total_rows`` to this rather than refusing
#: (``min(total_rows, 200)`` in its router). Silently is the problem: a
#: comparison of 250 frames would upload for several minutes and then be short
#: 50 of them with nothing said, so the check happens here, before anything is
#: sent.
MAX_ROWS = 200

#: Not measured. slow.pics' six was tuned against a real ban; nothing
#: comparable has been observed here, and this host publishes no rate limit at
#: all, so the number is deliberately below the one known to be survivable on
#: the busier service rather than above one known to be safe on this one.
MAX_PARALLEL_UPLOADS = 3

#: Attempts per image before giving up, covering rate limits and flaky links.
MAX_ATTEMPTS = 5

#: Minimum seconds between the start of one upload request and the next, across
#: all workers. Same reasoning as the parallelism above: unmeasured here, so it
#: matches the value that was measured to be enough on the busier service.
#:
#: Set to zero to disable, which is what the tests do.
MIN_UPLOAD_INTERVAL = 0.4

#: Ceiling for the retry backoff. Without one, exponential growth puts the last
#: attempt minutes away and the run looks hung.
MAX_BACKOFF = 30.0

#: Seconds for the small JSON calls. The image bodies get their own, sized to
#: the image, for the reasons set out in the slow.pics module.
_TIMEOUT = 30.0

#: Header carrying the token that authorises writes to an ownerless comparison.
EDIT_TOKEN_HEADER = "X-Edit-Token"


def _upload_timeout(size_bytes: int) -> float:
    """Seconds to allow for one image, sized to the image."""
    return max(90.0, 30.0 * size_bytes / 1_000_000)


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _host(base_url: str) -> str:
    """The host to name in an error, so a self-hosted instance names itself."""
    return urlsplit(base_url).netloc or base_url


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": f"kiyas/{__version__} (https://github.com/blast1see/kiyas)",
    }
    if api_key:
        # The raw key, with no "Bearer" prefix. The server reads the
        # Authorization header, tries it as a JWT only when it starts with
        # "Bearer ", and otherwise looks it up as an API key. Prefixing it
        # sends it down the JWT path, where it fails as a malformed token.
        headers["Authorization"] = api_key
    return headers


def resolve_base_url(base_url: str | None = None) -> str:
    """The instance to publish to: the argument, the environment, or comp.pics."""
    return (base_url or os.environ.get(HOST_URL_ENV) or BASE_URL).rstrip("/")


def resolve_api_key(api_key: str | None = None) -> str | None:
    """The key to publish with, or ``None`` to publish anonymously.

    Anonymous works: the comparison simply has no owner, and cannot be managed
    from an account afterwards. That is a real mode rather than a degraded one,
    so a missing key is not an error.
    """
    if api_key:
        return api_key
    return os.environ.get(API_KEY_ENV) or None


def snap_expiration(days: int) -> int:
    """The nearest day count the server will accept.

    ``--remove-after`` is a free integer because slow.pics takes one. Here it
    is an enum, and a value outside it fails validation *after* the comparison
    has been created, leaving an empty one behind. Ties go to the shorter
    option: a comparison that goes away sooner than asked is a smaller surprise
    than one that lingers.
    """
    return min(EXPIRATION_DAYS, key=lambda allowed: (abs(allowed - days), allowed))


@dataclass(frozen=True, slots=True)
class _Cell:
    """One image and where it belongs in the grid."""

    row: int
    column: int
    path: Path
    #: The source's name, not the frame's. The site reads its column headings
    #: off the first row's image names, so a frame label here would put
    #: "0:01:23 / 1234" above the column and lose which release is which.
    label: str


def build_metadata(
    comparison: Comparison,
    *,
    tags: tuple[str, ...] = (),
    expiration_days: int = 7,
    expiration_type: str = "from_last_access",
) -> dict:
    """The JSON body that creates the comparison."""
    if expiration_type not in EXPIRATION_TYPES:
        raise UploadError(
            f"unknown expiration type {expiration_type!r}; "
            f"expected one of {', '.join(EXPIRATION_TYPES)}"
        )
    return {
        "name": comparison.title,
        "show_name": comparison.title,
        "tags": list(tags),
        "total_rows": len(comparison.rows),
        "total_columns": len(comparison.sources),
        "expiration_type": expiration_type,
        "expiration_days": snap_expiration(expiration_days),
    }


def build_cells(comparison: Comparison) -> list[_Cell]:
    """Every image as a grid cell, in the order kiyas holds them.

    Source-major, because that is the order ``bbcode`` indexes: it reads
    ``urls[source_index * row_count + row_index]``. Collecting the resulting
    URLs in any other order pairs the wrong picture with the wrong source in
    the markup, and does it without erroring.

    ``row`` is the frame and ``column`` is the source -- the direct mapping,
    not the transposed one. See the module docstring.
    """
    return [
        _Cell(row=row_index, column=source_index, path=path, label=source.name)
        for source_index, source in enumerate(comparison.sources)
        for row_index, path in enumerate(source.images)
    ]


class _Pacer:
    """Keeps request starts a minimum interval apart, across every worker.

    The thread pool limits how many uploads run at once; this limits how fast
    they are allowed to *begin*. Those are different things, and only the
    second one is visible to the server as a burst.

    The sleep happens outside the lock on purpose. Holding it while waiting
    would make the workers queue up behind each other and turn the pool back
    into a single stream.
    """

    __slots__ = ("_interval", "_lock", "_next")

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            time.sleep(delay)


def _backoff(attempt: int) -> float:
    """Seconds to wait before retry ``attempt``, growing and jittered.

    Jitter matters more than the growth. Workers that hit the same rate limit
    at the same moment will, with a fixed delay, wake up together and reproduce
    the burst that caused it. The random half spreads them out.
    """
    ceiling = min(MAX_BACKOFF, 2.0 * 2**attempt)
    return ceiling * (0.5 + random.random() / 2)


def _retry_after(header: str | None, attempt: int) -> float:
    if header:
        try:
            return max(1.0, float(header))
        except ValueError:
            pass
    return _backoff(attempt)


def upload(
    comparison: Comparison,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    tags: tuple[str, ...] = (),
    expiration_days: int = 7,
    expiration_type: str = "from_last_access",
    progress=None,
    session=None,
) -> UploadResult:
    """Publish ``comparison`` and return its URL along with every image's URL.

    ``session`` exists so the tests can drive this without a network, matching
    the slow.pics backend so a caller can treat the two the same way.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise UploadError("the 'requests' package is required to publish") from exc

    base = resolve_base_url(base_url)
    api = f"{base}/api/v1"
    host = _host(base)

    # Checked before anything is sent. The server clamps instead of refusing,
    # so without this the failure mode is a comparison that uploads for minutes
    # and comes out quietly missing rows.
    if len(comparison.rows) > MAX_ROWS:
        raise UploadError(
            f"this comparison has {len(comparison.rows)} rows and {host} keeps at "
            f"most {MAX_ROWS}. It would silently drop the rest rather than refuse, "
            f"so nothing was sent."
        )

    # Built before the network is touched, so a bad expiration type costs
    # nothing rather than leaving an empty comparison behind.
    metadata = build_metadata(
        comparison,
        tags=tags,
        expiration_days=expiration_days,
        expiration_type=expiration_type,
    )

    client = session or requests.Session()
    client.headers.update(_headers(resolve_api_key(api_key)))

    if progress:
        progress(f"creating the comparison on {host}")

    created = None
    try:
        created = client.post(f"{api}/comparisons", json=metadata, timeout=_TIMEOUT)
        created.raise_for_status()
        response = created.json()
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same story
        raise UploadError(
            f"{host} refused the comparison: {exc}{transport.explain(created, host=host)}"
        ) from exc

    comparison_id = response.get("id")
    if not comparison_id:
        raise UploadError(f"{host} returned an unexpected response: {response!r}")

    # Newer instances gate writes to an ownerless comparison behind a token
    # handed out at creation. Older ones -- including the public instance at
    # the time of writing -- neither send one nor check for it, so this is
    # stored when offered and simply absent when it is not. Sending a header
    # the server does not read costs nothing; not sending one it does read
    # would fail every upload after the first release that adds it.
    edit_token = response.get("edit_token")

    # Measured against the public instance on 2026-08-21: a create carrying
    # expiration_days=1 came back, and stayed, at 7. The field is in the spec
    # and the current source honours it, so this is a gap in what that instance
    # is running rather than a wrong request -- but somebody who asked for one
    # day and got seven should be told by the thing that asked.
    notes: list[str] = []
    stored_days = response.get("expiration_days")
    if stored_days is not None and int(stored_days) != metadata["expiration_days"]:
        notes.append(
            f"asked {host} to keep this for {_days(metadata['expiration_days'])}; "
            f"it stored {_days(int(stored_days))}."
        )

    cells = build_cells(comparison)
    if progress:
        progress(f"uploading {len(cells)} images")

    filenames = _send_images(client, api, comparison_id, edit_token, cells, host, progress)

    return UploadResult(
        key=comparison_id,
        url=f"{base}/compare/{comparison_id}",
        uploaded=len(cells),
        skipped=0,
        image_urls=tuple(f"{base}/uploads/{comparison_id}/{name}" for name in filenames),
        notes=tuple(notes),
    )


def _send_images(client, api, comparison_id, edit_token, cells, host, progress) -> list[str]:
    """Upload every cell and return each one's stored filename, in cell order.

    The results go into a pre-sized list indexed by position rather than being
    appended as they finish: the pool completes them out of order, and the
    order is exactly what the markup depends on.
    """
    filenames: list[str | None] = [None] * len(cells)
    errors: list[str] = []
    done = 0
    total = len(cells)

    pacer = _Pacer(MIN_UPLOAD_INTERVAL)
    # Set by the first worker to be refused outright, so one refusal costs one
    # request rather than one per remaining image.
    blocked = threading.Event()

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_UPLOADS) as pool:
        futures = {
            pool.submit(
                _send_one, client, api, comparison_id, edit_token, cell, pacer, blocked, host
            ): index
            for index, cell in enumerate(cells)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                filenames[index] = future.result()
            except UploadError as exc:
                errors.append(f"{cells[index].path.name}: {exc}")
            done += 1
            if progress:
                progress(f"uploading {done}/{total}")

    if errors:
        shown = "\n  ".join(errors[:5])
        more = f"\n  ... and {len(errors) - 5} more" if len(errors) > 5 else ""
        raise UploadError(f"{len(errors)} image(s) failed to upload:\n  {shown}{more}")

    return [name for name in filenames if name is not None]


def _send_one(client, api, comparison_id, edit_token, cell, pacer, blocked, host) -> str:
    body = cell.path.read_bytes()
    timeout = _upload_timeout(len(body))

    fields = {
        "row": str(cell.row),
        "column": str(cell.column),
        "original_filename": cell.path.name,
        "custom_name": cell.label,
    }
    headers = {EDIT_TOKEN_HEADER: edit_token} if edit_token else {}

    for attempt in range(MAX_ATTEMPTS):
        if blocked.is_set():
            raise UploadError("not sent: the address was already refused")
        pacer.wait()

        try:
            response = client.post(
                f"{api}/comparison/{comparison_id}/image",
                data=fields,
                files={"file": (cell.path.name, body, "image/png")},
                headers=headers,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - retry transport failures
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(str(exc)) from exc
            time.sleep(_backoff(attempt))
            continue

        if response.status_code == 429:
            time.sleep(_retry_after(response.headers.get("Retry-After"), attempt))
            continue

        if response.status_code == 403:
            blocked.set()
            raise UploadError(
                f"refused{transport.explain(response, host=host) or ': HTTP 403'}. An "
                f"instance that gates anonymous uploads needs an API key in "
                f"{API_KEY_ENV}."
            )

        if response.status_code >= 500:
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(f"HTTP {response.status_code}")
            time.sleep(_backoff(attempt))
            continue

        if response.status_code >= 400:
            raise UploadError(
                f"HTTP {response.status_code}{transport.explain(response, host=host)}"
            )

        try:
            filename = response.json().get("filename")
        except Exception as exc:  # noqa: BLE001 - a 200 with no JSON is still a failure
            raise UploadError(f"stored, but the reply was not readable: {exc}") from exc
        if not filename:
            # Without a name there is no address for this image, and the markup
            # formats would come out one URL short of the grid they describe.
            raise UploadError("stored, but the server did not say under what name")
        return filename

    raise UploadError("gave up after repeated rate limiting")
