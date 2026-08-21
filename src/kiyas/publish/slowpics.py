"""Uploading a comparison to slow.pics.

The endpoints are not documented anywhere public, so the shapes below were read
off a working client (vsview-comp) rather than guessed. The flow is:

1. ``GET /comparison`` to pick up the ``XSRF-TOKEN`` and ``BROWSER-ID`` cookies.
   The token goes back as a header on every write; the browser id is what ties
   a collection to you well enough to edit it later without an account.
2. Hash every image and send the digests along with the collection metadata to
   ``POST /upload/comparison``. The server answers with a UUID per image and a
   list of the ones it already has -- that list is why re-running a failed
   upload does not re-send everything.
3. ``POST /upload/image/{uuid}`` for each image that is not already there.

Two responses mean success even though they do not look like it: a 400 whose
``X-Error-Message`` is ``IMAGE_IS_COMPLETE`` (the server already had that exact
image), and a 429, which only means slow down. Everything else is a real error
and stops the upload rather than leaving a half-populated comparison.
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import __version__
from . import transport
from .manifest import Comparison
from .result import UploadError, UploadResult

#: Re-exported so callers and tests keep reaching for it here. Assigned rather
#: than imported because nothing in this module uses it directly any more.
_CF_CODE = transport._CF_CODE

BASE_URL = "https://slow.pics"

#: slow.pics is a free service run by one person. Six at a time is what the
#: reference client uses; going wider mostly earns 429s.
MAX_PARALLEL_UPLOADS = 6

#: Attempts per image before giving up, covering rate limits and flaky links.
MAX_ATTEMPTS = 5

#: Minimum seconds between the start of one upload request and the next, across
#: all workers.
#:
#: Six workers with nothing holding them back open six connections in the same
#: instant, and do it again every time one finishes. Measured on a real run --
#: 24 screenshots of about 6 MB -- that burst plus the retries it caused was
#: enough for slow.pics to ban the address outright (Cloudflare 1006), which no
#: amount of retrying recovers from. Spacing the starts costs about ten seconds
#: on a 24-image comparison, against uploads that take half a minute each.
#:
#: Set to zero to disable, which is what the tests do.
MIN_UPLOAD_INTERVAL = 0.4

#: Ceiling for the retry backoff. Without one, exponential growth puts the last
#: attempt minutes away and the run looks hung.
MAX_BACKOFF = 30.0


def _upload_timeout(size_bytes: int) -> float:
    """Seconds to allow for one image, sized to the image.

    One constant used to cover both the small API calls and the image bodies,
    and thirty seconds is generous for the first and hopeless for the second.
    Measured on a real comparison: 24 screenshots of about 6 MB each, nine of
    them lost to "the write operation timed out".

    The arithmetic that matters is the parallelism. Six uploads share one
    uplink, so each gets roughly a sixth of it, and a 6 MB image at a sixth of
    a modest home connection is already at thirty seconds before anything goes
    wrong. This allows about 34 kB/s per stream -- slow, but a slow link should
    finish rather than fail, because failing costs the whole run.
    """
    return max(90.0, 30.0 * size_bytes / 1_000_000)


_TIMEOUT = 30.0


#: What slow.pics accepts as a TMDB reference. The reference client documents it
#: as "TV_XXXXXX" or "MOVIE_XXXXXX"; a bare number is refused, and refused with
#: a bare 400 carrying no body and no explanation, which loses the whole upload.
_TMDB_PATTERN = re.compile(r"^(movie|tv)[_/:\s-]*(\d+)$", re.IGNORECASE)


def normalise_tmdb(value: str) -> str:
    """Turn a TMDB reference into the form slow.pics wants, or refuse it.

    Accepts the shapes people actually have to hand -- ``MOVIE_1275779``,
    ``movie/1275779`` (which is how Matroska tags carry it), ``tv 1399`` -- and
    returns the canonical ``MOVIE_1275779``.

    A bare number is refused rather than assumed. The type is not decoration:
    guessing wrong attaches the comparison to a different title, and the person
    who passed the id is the one who knows which it is.
    """
    text = str(value).strip()
    match = _TMDB_PATTERN.match(text)
    if match:
        return f"{match.group(1).upper()}_{match.group(2)}"
    if text.isdigit():
        raise ValueError(
            f"--tmdb {text} does not say whether that is a film or a series. "
            f"slow.pics wants MOVIE_{text} or TV_{text}."
        )
    raise ValueError(
        f"--tmdb {text!r} is not a TMDB reference. Expected MOVIE_1275779, "
        f"TV_1399, or the movie/1275779 form Matroska tags use."
    )


def _headers() -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/comparison",
        # Identifying the client honestly is the least a tool can do when it is
        # uploading to someone else's free service.
        "User-Agent": f"kiyas/{__version__} (https://github.com/blast1see/kiyas)",
    }


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_payload(
    comparison: Comparison,
    *,
    public: bool = False,
    nsfw: bool = False,
    optimize: bool = True,
    remove_after_days: int = 0,
    tmdb_id: str | None = None,
) -> dict[str, str]:
    """The form fields describing the collection, without the images themselves."""
    payload: dict[str, str] = {
        "collectionName": comparison.title,
        # PNG in, PNG out. Optimisation of a PNG is lossless recompression, so
        # it changes the file without changing a pixel -- which is the only
        # kind of change a comparison can tolerate.
        "optimizeImages": str(optimize).lower(),
        "desiredFileType": "image/png",
        "hentai": str(nsfw).lower(),
        "public": str(public).lower(),
        # Unlisted by default. A comparison is usually a working document, and
        # publishing one to the site's front page is a decision, not a default.
        "visibility": "PUBLIC" if public else "LINK_ONLY",
        "removeAfter": str(remove_after_days) if remove_after_days >= 1 else "",
    }

    if comparison.is_comparison:
        payload |= {"canvasMode": "none", "imageFit": "none"}

    for index, row in enumerate(comparison.rows):
        if comparison.is_comparison:
            payload[f"comparisons[{index}].name"] = row.label
            payload[f"comparisons[{index}].hentai"] = str(nsfw).lower()
            for source_index, source in enumerate(comparison.sources):
                payload[f"comparisons[{index}].images[{source_index}].name"] = source.name
        else:
            source = comparison.sources[0]
            payload[f"images[{index}].name"] = f"{row.label} - {source.name}"

    if tmdb_id:
        payload["tmdbId"] = normalise_tmdb(tmdb_id)
    return payload


def _edge_block(response) -> str:
    """One sentence for a Cloudflare refusal, or "" if this is not one."""
    return transport.edge_block(response, host="slow.pics")


def _explain(response) -> str:
    """Why a request was refused: the edge's reason if it was the edge, else the body."""
    return transport.explain(response, host="slow.pics")


def build_hashes(comparison: Comparison) -> dict[str, str]:
    """Digest every image, keyed the way the server expects.

    Sending these up front is what lets the server say "I already have that
    one", so a re-run after a failed upload only sends what is missing.
    """
    hashes: dict[str, str] = {}
    for row_index in range(len(comparison.rows)):
        for source_index, source in enumerate(comparison.sources):
            path = source.images[row_index]
            key = (
                f"comparisons[{row_index}].images[{source_index}].hashSum"
                if comparison.is_comparison
                else f"images[{row_index}].hashSum"
            )
            hashes[key] = _digest(path)
    return hashes


def _images_to_send(comparison: Comparison, response: dict) -> list[tuple[str, Path]]:
    """Pair each image UUID the server handed back with the file it wants.

    The grid is transposed between the two sides: the server returns
    ``images[frame][source]`` while kiyas holds ``sources[source][frame]``.
    Getting this backwards uploads every picture into the wrong cell, and the
    result looks like a genuine difference between the releases.
    """
    grid = response.get("images") or []
    already = set(response.get("completeImageUuids") or [])

    pending: list[tuple[str, Path]] = []
    for source_index, source in enumerate(comparison.sources):
        for row_index, path in enumerate(source.images):
            try:
                image_uuid = (
                    grid[row_index][source_index]
                    if comparison.is_comparison
                    else grid[0][row_index]
                )
            except (IndexError, TypeError) as exc:
                raise UploadError(
                    f"slow.pics returned an image grid that does not match the "
                    f"comparison: it sent {len(grid)} rows, this has {len(comparison.rows)}"
                ) from exc
            if image_uuid not in already:
                pending.append((image_uuid, path))
    return pending


def upload(
    comparison: Comparison,
    *,
    public: bool = False,
    nsfw: bool = False,
    optimize: bool = True,
    remove_after_days: int = 0,
    tmdb_id: str | None = None,
    browser_id: str | None = None,
    progress=None,
    session=None,
) -> UploadResult:
    """Publish ``comparison`` and return its URL.

    ``session`` exists so the tests can drive this without a network.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise UploadError("the 'requests' package is required to publish") from exc

    client = session or requests.Session()
    client.headers.update(_headers())

    if progress:
        progress("connecting to slow.pics")

    landing = None
    try:
        landing = client.get(f"{BASE_URL}/comparison", timeout=_TIMEOUT)
        landing.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same story
        raise UploadError(f"could not reach slow.pics: {exc}{_explain(landing)}") from exc

    token = client.cookies.get("XSRF-TOKEN")
    if not token:
        raise UploadError(
            "slow.pics did not hand out an XSRF token. The site may be down, or "
            "something between here and it is rewriting cookies."
        )
    client.headers["X-XSRF-TOKEN"] = token

    browser_id = browser_id or client.cookies.get("BROWSER-ID") or str(uuid.uuid4())

    if progress:
        progress(f"hashing {comparison.total_images} images")
    data = build_payload(
        comparison,
        public=public,
        nsfw=nsfw,
        optimize=optimize,
        remove_after_days=remove_after_days,
        tmdb_id=tmdb_id,
    )
    data |= build_hashes(comparison)
    data["browserId"] = browser_id

    endpoint = "comparison" if comparison.is_comparison else "collection"
    if progress:
        progress("creating the collection")
    created = None
    try:
        created = client.post(f"{BASE_URL}/upload/{endpoint}", data=data, timeout=_TIMEOUT)
        created.raise_for_status()
        response = created.json()
    except Exception as exc:  # noqa: BLE001
        raise UploadError(f"slow.pics refused the collection: {exc}{_explain(created)}") from exc

    key = response.get("key")
    collection_uuid = response.get("collectionUuid")
    if not key or not collection_uuid:
        raise UploadError(f"slow.pics returned an unexpected response: {response!r}")

    pending = _images_to_send(comparison, response)
    skipped = comparison.total_images - len(pending)
    url = f"{BASE_URL}/c/{key}"

    if pending:
        _send_images(client, collection_uuid, browser_id, pending, progress)

    return UploadResult(key=key, url=url, uploaded=len(pending), skipped=skipped)


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

    Jitter matters more than the growth here. Six workers that hit the same
    rate limit at the same moment will, with a fixed delay, wake up together
    and reproduce the burst that caused it. The random half spreads them out.
    """
    ceiling = min(MAX_BACKOFF, 2.0 * 2**attempt)
    return ceiling * (0.5 + random.random() / 2)


def _send_images(client, collection_uuid, browser_id, pending, progress) -> None:
    done = 0
    total = len(pending)
    errors: list[str] = []

    pacer = _Pacer(MIN_UPLOAD_INTERVAL)
    # Set by the first worker to be refused. Every other worker checks it
    # before sending and gives up instead, so one refusal costs one request
    # rather than one per remaining image. Not retrying a 403 fixed the
    # per-image multiplier; this fixes the per-comparison one -- a 24-image
    # comparison against a blocked address was still 24 requests, all of them
    # arriving after the answer was already known.
    blocked = threading.Event()

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_UPLOADS) as pool:
        futures = {
            pool.submit(
                _send_one, client, collection_uuid, browser_id, image_uuid, path, pacer, blocked
            ): path
            for image_uuid, path in pending
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                future.result()
            except UploadError as exc:
                errors.append(f"{path.name}: {exc}")
            done += 1
            if progress:
                progress(f"uploading {done}/{total}")

    if errors:
        shown = "\n  ".join(errors[:5])
        more = f"\n  ... and {len(errors) - 5} more" if len(errors) > 5 else ""
        raise UploadError(f"{len(errors)} image(s) failed to upload:\n  {shown}{more}")


def _send_one(
    client,
    collection_uuid: str,
    browser_id: str,
    image_uuid: str,
    path: Path,
    pacer: _Pacer | None = None,
    blocked: threading.Event | None = None,
) -> None:
    fields = {
        "collectionUuid": collection_uuid,
        "imageUuid": image_uuid,
        "browserId": browser_id,
    }

    body = path.read_bytes()
    timeout = _upload_timeout(len(body))

    for attempt in range(MAX_ATTEMPTS):
        # Checked before the pacer, so a worker that has been waiting its turn
        # while another was refused does not go on to spend that turn.
        if blocked is not None and blocked.is_set():
            raise UploadError("not sent: the address was already refused")

        # Before every attempt, not just the first: a retry is another request
        # arriving at the same server, and retries are what turn a slow link
        # into a burst.
        if pacer is not None:
            pacer.wait()

        try:
            response = client.post(
                f"{BASE_URL}/upload/image/{image_uuid}",
                data=fields,
                files={"file": (path.name, body, "image/png")},
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - retry transport failures
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(str(exc)) from exc
            time.sleep(_backoff(attempt))
            continue

        if response.status_code == 400:
            # The server already holds this exact image. That is a success:
            # the hash matched, so the picture is in the collection.
            if response.headers.get("X-Error-Message") == "IMAGE_IS_COMPLETE":
                return
            raise UploadError(response.headers.get("X-Error-Message") or "rejected by slow.pics")

        if response.status_code == 429:
            wait = _retry_after(response.headers.get("Retry-After"), attempt)
            time.sleep(wait)
            continue

        if response.status_code == 403:
            # Not retried, and this is the important one. A 403 is the edge
            # refusing the address, not the server being busy; five more
            # attempts per image cannot succeed and are themselves the traffic
            # that turns a rate limit into a ban. Six workers each retrying
            # into a block is how the address got refused in the first place.
            #
            # Telling the other workers stops the remaining images too: the
            # answer is about the address, so it is the same answer for all of
            # them and there is nothing to learn by asking again.
            if blocked is not None:
                blocked.set()
            raise UploadError(f"refused{_explain(response) or ': HTTP 403'}")

        if response.status_code >= 400:
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(f"HTTP {response.status_code}")
            time.sleep(_backoff(attempt))
            continue

        return

    raise UploadError("gave up after repeated rate limiting")


def _retry_after(header: str | None, attempt: int) -> float:
    """Honour the server's Retry-After, falling back to a linear backoff."""
    try:
        return max(1.0, float(header))
    except (TypeError, ValueError):
        return _backoff(attempt)
