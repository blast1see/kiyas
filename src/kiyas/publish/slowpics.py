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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from .manifest import Comparison

BASE_URL = "https://slow.pics"

#: slow.pics is a free service run by one person. Six at a time is what the
#: reference client uses; going wider mostly earns 429s.
MAX_PARALLEL_UPLOADS = 6

#: Attempts per image before giving up, covering rate limits and flaky links.
MAX_ATTEMPTS = 5

_TIMEOUT = 30.0


class UploadError(RuntimeError):
    """Raised when a comparison cannot be published."""


@dataclass(frozen=True, slots=True)
class UploadResult:
    key: str
    url: str
    uploaded: int
    skipped: int


#: How much of a failed response to quote back. Enough for a JSON field error,
#: short of an HTML error page.
_ERROR_BODY_LIMIT = 600


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
        payload["tmdbId"] = tmdb_id
    return payload


def _server_said(response) -> str:
    """The body of a failed response, which is where the reason lives.

    ``raise_for_status`` produces "400 Client Error: Bad Request for url: ...",
    which says a field was wrong and not which one. The server does say, in the
    body, and that was being thrown away -- leaving a caller with a failure they
    cannot act on and no way to find out more except reading this source.

    Truncated because a server having a bad day can answer with an HTML error
    page, and a wall of markup in a one-line error is its own kind of unhelpful.
    """
    if response is None:
        return ""
    body = (getattr(response, "text", "") or "").strip()
    if not body:
        return ""
    if len(body) > _ERROR_BODY_LIMIT:
        body = body[:_ERROR_BODY_LIMIT] + "..."
    return "\nthe server said: " + body


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

    try:
        landing = client.get(f"{BASE_URL}/comparison", timeout=_TIMEOUT)
        landing.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same story
        raise UploadError(f"could not reach slow.pics: {exc}") from exc

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
        raise UploadError(
            f"slow.pics refused the collection: {exc}{_server_said(created)}"
        ) from exc

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


def _send_images(client, collection_uuid, browser_id, pending, progress) -> None:
    done = 0
    total = len(pending)
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_UPLOADS) as pool:
        futures = {
            pool.submit(_send_one, client, collection_uuid, browser_id, image_uuid, path): path
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


def _send_one(client, collection_uuid: str, browser_id: str, image_uuid: str, path: Path) -> None:
    fields = {
        "collectionUuid": collection_uuid,
        "imageUuid": image_uuid,
        "browserId": browser_id,
    }

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.post(
                f"{BASE_URL}/upload/image/{image_uuid}",
                data=fields,
                files={"file": (path.name, path.read_bytes(), "image/png")},
                timeout=_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - retry transport failures
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(str(exc)) from exc
            time.sleep((attempt + 1) * 2)
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

        if response.status_code >= 400:
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(f"HTTP {response.status_code}")
            time.sleep((attempt + 1) * 2)
            continue

        return

    raise UploadError("gave up after repeated rate limiting")


def _retry_after(header: str | None, attempt: int) -> float:
    """Honour the server's Retry-After, falling back to a linear backoff."""
    try:
        return max(1.0, float(header))
    except (TypeError, ValueError):
        return (attempt + 1) * 2.0
