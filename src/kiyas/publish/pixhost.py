"""Publishing to pixhost.to: an image host, not a comparison host.

The difference matters and is the reason this backend looks different from the
other two. slow.pics and comp.pics store a *comparison* -- a grid they know the
shape of, with a viewer that flips between sources at one frame. pixhost stores
pictures. What it gives back for each one is a page and a thumbnail, which is
exactly what a forum post wants and exactly what neither of the other two hands
over.

So this is the backend for a set of screenshots that is going into a post. For
a comparison it is the wrong shape -- there is no viewer at the other end --
and publishing one says so rather than refusing, because a screenshot set of
two releases is a real thing somebody may want hosted.

Three facts from the API documentation shape the code:

- **No API key.** "API v2 does not require an API key." There is no credential
  to store, which is why this could be added without giving kiyas the
  credential store it has so far managed without.
- **10 MB per image, refused with a 413.** A UHD PNG passes that regularly, so
  the size check happens before anything is sent rather than after twenty
  images have already gone up.
- **``show_url`` is an HTML page, not a picture, and the direct address of the
  full-size file is not documented.** So ``image_urls`` stays empty here: it is
  documented as a *direct* URL, ``bbcode`` puts it inside ``[img]``, and an
  HTML page inside an ``[img]`` tag is a broken picture on every forum there
  is. What gets filled in instead is ``page_urls`` and ``thumbnail_urls``, both
  documented fields, and the markup that pairs them.

``optimize_for_web`` is never sent. Its default is off, and with it off the
file is stored unmodified -- the only condition under which a lossless PNG
screenshot survives being hosted at all.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from . import transport
from .manifest import Comparison
from .result import UploadError, UploadResult

BASE_URL = "https://api.pixhost.to"

#: The name to use when talking *about* the host rather than to it.
HOST = "pixhost.to"

#: Documented limit, per image. Refused with a 413.
#:
#: Ten million bytes, not ten mebibytes. The documentation says "10 MB" and
#: does not say which it means; the two are half a megabyte apart, and a file
#: in that gap is exactly the one this check exists to stop. Being wrong the
#: cautious way costs a file that might have fitted, and says so before
#: anything is sent. Being wrong the other way costs a half-filled gallery
#: found out on image twenty-one.
MAX_IMAGE_BYTES = 10_000_000

#: No published rate limit for uploads -- the documentation gives one only for
#: the status endpoint. Unmeasured here, so this copies the value the other two
#: backends arrived at rather than inventing a wider one: being wrong in this
#: direction costs seconds, and being wrong in the other direction cost the
#: slow.pics backend an address ban once already.
MAX_PARALLEL_UPLOADS = 3

#: Attempts per image before giving up, covering rate limits and flaky links.
MAX_ATTEMPTS = 5

#: Minimum seconds between the start of one upload request and the next, across
#: all workers. Set to zero to disable, which is what the tests do.
MIN_UPLOAD_INTERVAL = 0.4

#: Thumbnail width to ask for. The API accepts 150-500 and defaults to 200. 350
#: is large enough to tell two encodes apart at a glance and small enough that
#: a dozen of them load.
THUMB_SIZE = 350

#: The range the API documents for ``max_th_size``.
THUMB_RANGE = (150, 500)

#: Seconds for the small JSON calls. Image bodies get their own, sized to the
#: image, from `transport.upload_timeout`.
_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class _Image:
    """One picture to send, and where it sits in kiyas' own order."""

    path: Path
    position: int


@dataclass(frozen=True, slots=True)
class _Stored:
    page_url: str
    thumbnail_url: str


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": f"kiyas/{__version__} (https://github.com/blast1see/kiyas)",
    }


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def build_images(comparison: Comparison) -> list[_Image]:
    """Every image in the order kiyas holds them: source-major.

    ``bbcode`` reads ``urls[source_index * row_count + row_index]``. Collecting
    the addresses in any other order pairs the wrong picture with the wrong
    source in the markup, and does it without erroring.
    """
    return [
        _Image(path=path, position=source_index * len(comparison.rows) + row_index)
        for source_index, source in enumerate(comparison.sources)
        for row_index, path in enumerate(source.images)
    ]


def oversized(comparison: Comparison) -> list[tuple[Path, int]]:
    """Images this host will refuse, with their sizes, largest first."""
    too_big = [
        (path, path.stat().st_size)
        for source in comparison.sources
        for path in source.images
        if path.stat().st_size > MAX_IMAGE_BYTES
    ]
    return sorted(too_big, key=lambda item: item[1], reverse=True)


def upload(
    comparison: Comparison,
    *,
    nsfw: bool = False,
    thumb_size: int = THUMB_SIZE,
    progress=None,
    session=None,
) -> UploadResult:
    """Publish ``comparison`` as a gallery and return every image's addresses.

    ``session`` exists so the tests can drive this without a network, matching
    the other two backends so a caller can treat all three the same way.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a hard dependency
        raise UploadError("the 'requests' package is required to publish") from exc

    low, high = THUMB_RANGE
    if not low <= thumb_size <= high:
        raise UploadError(f"{HOST} accepts a thumbnail size between {low} and {high} pixels.")

    # Checked before anything is sent. Twenty images can go up before the
    # twenty-first is refused, and a half-filled gallery is worse than none.
    refused = oversized(comparison)
    if refused:
        shown = "\n  ".join(
            f"{path.name} is {size / 1_000_000:.1f} MB" for path, size in refused[:5]
        )
        more = f"\n  ... and {len(refused) - 5} more" if len(refused) > 5 else ""
        plural = "is" if len(refused) == 1 else "are"
        raise UploadError(
            f"{HOST} takes at most {MAX_IMAGE_BYTES // 1_000_000} MB per image, and "
            f"{_count(len(refused), 'image')} here {plural} over it:\n  {shown}{more}\n"
            f"Nothing was sent. Capture smaller frames with 'resize' on the source, or "
            f"publish somewhere that takes them whole."
        )

    notes: list[str] = []
    if comparison.is_comparison:
        notes.append(
            f"{HOST} hosts pictures, not comparisons: nothing at the other end flips "
            f"between the sources at one frame."
        )

    client = session or requests.Session()
    client.headers.update(_headers())

    if progress:
        progress(f"creating the gallery on {HOST}")
    gallery = _create_gallery(client, comparison.title)
    manage_url = gallery.get("manage_url")

    images = build_images(comparison)
    if progress:
        progress(f"uploading {len(images)} images")
    try:
        stored = _send_images(client, gallery, images, nsfw, thumb_size, progress)
    except UploadError as exc:
        # Some of them are already up by now, on a host with no expiry, and the
        # only way to take them down is the token the gallery handed out once.
        # Letting the failure carry it away is how a failed run leaves rubbish
        # nobody can clear.
        if manage_url:
            raise UploadError(f"{exc}\n\nWhat did go up can be removed here: {manage_url}") from exc
        raise

    if progress:
        progress("publishing the gallery")
    refusal = _finalise(client, gallery)
    if refusal:
        notes.append(refusal)

    if manage_url:
        notes.append(f"keep this if you may want the gallery gone later: {manage_url}")

    return UploadResult(
        key=gallery["gallery_hash"],
        url=gallery["gallery_url"],
        uploaded=len(images),
        skipped=0,
        page_urls=tuple(item.page_url for item in stored),
        thumbnail_urls=tuple(item.thumbnail_url for item in stored),
        notes=tuple(notes),
    )


def _create_gallery(client, title: str) -> dict:
    created = None
    try:
        created = client.post(
            f"{BASE_URL}/galleries",
            # include_manage_url is asked for because without it there is no
            # way to take a gallery down again: the API deletes by a token it
            # only ever hands out here, and nothing else can recover it.
            data={"gallery_name": title, "include_manage_url": "1"},
            timeout=_TIMEOUT,
        )
        created.raise_for_status()
        answer = created.json()
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same story
        raise UploadError(
            f"{HOST} refused the gallery: {exc}{transport.explain(created, host=HOST)}"
        ) from exc

    wanted = ("gallery_hash", "gallery_upload_hash", "gallery_url")
    missing = [key for key in wanted if not answer.get(key)]
    if missing:
        raise UploadError(f"{HOST} returned a gallery without {', '.join(missing)}: {answer!r}")
    return answer


def _finalise(client, gallery: dict) -> str:
    """Publish the gallery, reporting rather than raising if that is refused.

    By this point every picture is up and every page and thumbnail address is
    known, which is what the markup is made of. Throwing all of that away
    because the last call of three was refused would be the wrong trade, so it
    comes back as a note about what the server did.
    """
    try:
        response = client.post(
            f"{BASE_URL}/galleries/{gallery['gallery_hash']}/finalize",
            data={"gallery_upload_hash": gallery["gallery_upload_hash"]},
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - the images are already up
        return f"the images are up, but {HOST} could not be asked to publish the gallery: {exc}"
    if response.status_code >= 400:
        return (
            f"the images are up, but {HOST} refused to publish the gallery "
            f"(HTTP {response.status_code}). The image links still work."
        )
    return ""


def _send_images(client, gallery, images, nsfw, thumb_size, progress) -> list[_Stored]:
    """Upload every image and return its addresses, in kiyas' own order.

    The results go into a pre-sized list indexed by position rather than being
    appended as they finish: the pool completes them out of order, and the
    order is exactly what the markup depends on.
    """
    stored: list[_Stored | None] = [None] * len(images)
    errors: list[str] = []
    done = 0
    total = len(images)

    pacer = transport.Pacer(MIN_UPLOAD_INTERVAL)
    # Set by the first worker to be refused outright, so one refusal costs one
    # request rather than one per remaining image.
    blocked = threading.Event()

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_UPLOADS) as pool:
        futures = {
            pool.submit(
                _send_one, client, gallery, image, nsfw, thumb_size, pacer, blocked
            ): image.position
            for image in images
        }
        for future in as_completed(futures):
            position = futures[future]
            try:
                stored[position] = future.result()
            except UploadError as exc:
                errors.append(f"{images[position].path.name}: {exc}")
            done += 1
            if progress:
                progress(f"uploading {done}/{total}")

    if errors:
        shown = "\n  ".join(errors[:5])
        more = f"\n  ... and {len(errors) - 5} more" if len(errors) > 5 else ""
        raise UploadError(f"{_count(len(errors), 'image')} failed to upload:\n  {shown}{more}")

    return [item for item in stored if item is not None]


def _send_one(client, gallery, image, nsfw, thumb_size, pacer, blocked) -> _Stored:
    body = image.path.read_bytes()
    timeout = transport.upload_timeout(len(body))

    fields = {
        # Required: the API refuses an upload without it rather than assuming.
        "content_type": "1" if nsfw else "0",
        "max_th_size": str(thumb_size),
        "gallery_hash": gallery["gallery_hash"],
        "gallery_upload_hash": gallery["gallery_upload_hash"],
    }

    for attempt in range(MAX_ATTEMPTS):
        if blocked.is_set():
            raise UploadError("not sent: the address was already refused")
        pacer.wait()

        try:
            response = client.post(
                f"{BASE_URL}/images",
                data=fields,
                files={"img": (image.path.name, body, "image/png")},
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - retry transport failures
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(str(exc)) from exc
            time.sleep(transport.backoff(attempt))
            continue

        if response.status_code == 429:
            time.sleep(transport.retry_after(response.headers.get("Retry-After"), attempt))
            continue

        if response.status_code == 403:
            blocked.set()
            raise UploadError(f"refused{transport.explain(response, host=HOST) or ': HTTP 403'}")

        if response.status_code == 413:
            # The check before the run should have caught this, so arriving
            # here means the host's limit moved rather than that the check was
            # skipped. Worth saying plainly instead of retrying.
            raise UploadError(
                f"refused as too large, although it is under the "
                f"{MAX_IMAGE_BYTES // 1_000_000} MB {HOST} documents"
            )

        if response.status_code == 414:
            raise UploadError(f"{HOST} does not accept this image format")

        if response.status_code >= 500:
            if attempt == MAX_ATTEMPTS - 1:
                raise UploadError(f"HTTP {response.status_code}")
            time.sleep(transport.backoff(attempt))
            continue

        if response.status_code >= 400:
            raise UploadError(
                f"HTTP {response.status_code}{transport.explain(response, host=HOST)}"
            )

        try:
            answer = response.json()
        except Exception as exc:  # noqa: BLE001 - a 200 with no JSON is still a failure
            raise UploadError(f"stored, but the reply was not readable: {exc}") from exc

        page_url = answer.get("show_url")
        thumbnail_url = answer.get("th_url")
        if not page_url or not thumbnail_url:
            # Without both there is no markup for this image, and the formats
            # would come out one pair short of the grid they describe.
            raise UploadError("stored, but the server did not say where")
        return _Stored(page_url=page_url, thumbnail_url=thumbnail_url)

    raise UploadError("gave up after repeated rate limiting")
