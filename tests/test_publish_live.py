"""Publishing for real, against the actual hosts.

Never run in CI, and never run by accident: every test here uploads something
to a free service somebody else pays for. `pytest -m live` opts in.

These exist because the fake sessions in the other publish tests can only prove
that kiyas sends what kiyas thinks it sends. They cannot say whether the host
accepts it, whether the addresses that come back resolve, or whether the
picture at the far end is the one that was meant to be there -- and that last
one is the failure this project keeps finding: an upload that succeeds and puts
the wrong picture in the wrong place.

The images are generated here with ffmpeg and are two seconds of `testsrc2`.
Nothing from anybody's library goes to a third party from a test run: the rule
is in CLAUDE.md and this file is the reason it is worth stating.

The grid is deliberately the smallest that can catch a transposition: two
sources, one visibly brighter than the other, so a swapped pair is a number
that comes out the wrong way round rather than something a human has to spot.
"""

from __future__ import annotations

import io
import subprocess

import pytest

from kiyas.media import binaries
from kiyas.publish import comppics, load_manifest, pixhost, slowpics

pytestmark = pytest.mark.live

#: How much brighter the second column is. Large enough to survive the
#: thumbnail resize the hosts do, small enough to stay a plausible encode.
BRIGHTNESS = 0.08


def _requests():
    return pytest.importorskip("requests")


def _numpy():
    return pytest.importorskip("numpy")


def _image():
    return pytest.importorskip("PIL.Image")


@pytest.fixture(scope="module")
def published_set(tmp_path_factory):
    """A real two-column comparison on disk, made from synthetic clips."""
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    ffmpeg = binaries.require_binary("ffmpeg")
    directory = tmp_path_factory.mktemp("live")

    from kiyas import config, run

    for name, extra in (("a", []), ("b", ["-vf", f"eq=brightness={BRIGHTNESS}"])):
        subprocess.run(
            [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=4",
                *extra,
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "12", "-bf", "3",
                "-pix_fmt", "yuv420p", str(directory / f"{name}.mkv"),
            ],  # fmt: skip
            check=True, capture_output=True, timeout=300,
        )  # fmt: skip

    project = directory / "live.toml"
    project.write_text(
        "\n".join(
            [
                'title = "kiyas live check"',
                'engine = "ffmpeg"',
                "",
                "[frames]",
                'method = "count"',
                "count = 2",
                "b_frames_only = false",
                "skip_dark = false",
                "",
                "[[source]]",
                f'path = "{(directory / "a.mkv").as_posix()}"',
                'name = "Reference"',
                "",
                "[[source]]",
                f'path = "{(directory / "b.mkv").as_posix()}"',
                'name = "Brighter"',
                "",
                "[output]",
                f'directory = "{(directory / "out").as_posix()}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    run.run(config.load(project))
    return load_manifest(directory / "out")


def _means(comparison):
    """Mean brightness of every local image, keyed by (column, row)."""
    np = _numpy()
    Image = _image()
    return {
        (source.name, row.label): float(
            np.asarray(Image.open(path).convert("L"), dtype=np.float32).mean()
        )
        for source in comparison.sources
        for row, path in zip(comparison.rows, source.images, strict=True)
    }


def _fetched_mean(session, url):
    np = _numpy()
    Image = _image()
    response = session.get(url, timeout=60)
    assert response.status_code == 200, f"{url} answered {response.status_code}"
    assert response.headers.get("Content-Type", "").startswith("image"), (
        f"{url} is {response.headers.get('Content-Type')!r}, not a picture"
    )
    return float(
        np.asarray(Image.open(io.BytesIO(response.content)).convert("L"), dtype=np.float32).mean()
    )


def _assert_each_address_holds_its_own_picture(comparison, urls):
    """Every returned address must serve the image kiyas put there.

    Compared by brightness rather than by bytes because the hosts resize, so
    the thumbnail is never the file that was sent. Brightness survives that,
    and it is enough: the two columns differ by construction.
    """
    requests = _requests()
    session = requests.Session()
    session.headers["User-Agent"] = "kiyas-live-check"
    local = _means(comparison)
    rows = len(comparison.rows)

    for source_index, source in enumerate(comparison.sources):
        for row_index, row in enumerate(comparison.rows):
            served = _fetched_mean(session, urls[source_index * rows + row_index])
            here = local[(source.name, row.label)]
            other = local[(comparison.sources[1 - source_index].name, row.label)]
            assert abs(served - here) < abs(served - other), (
                f"{source.name} at {row.label} served something closer to the other "
                f"column (got {served:.1f}, this column is {here:.1f}, the other "
                f"is {other:.1f})"
            )


def _delete_pixhost(result) -> bool:
    """Take the gallery down again, using the token the upload reported.

    The other two hosts are given an expiry; this one has none, so without this
    every `-m live` run would leave a permanent public gallery behind on a free
    service. The token appears only in the notes, which is why the note exists.

    Returns rather than raises, and is called from a `finally`: raising there
    would replace whatever the round-trip check was failing about with a
    message about the cleanup.
    """
    requests = _requests()
    for note in result.notes:
        _, _, tail = note.partition("pixhost.to/manage/")
        if not tail:
            continue
        try:
            response = requests.post(
                f"{pixhost.BASE_URL}/management/{tail.strip()}/delete",
                headers={"Accept": "application/json"},
                timeout=60,
            )
            return bool(response.json().get("deleted"))
        except Exception:  # noqa: BLE001 - reported by the caller, not raised here
            return False
    return False


def test_pixhost_round_trip(published_set):
    """Upload, read every thumbnail back, then take the gallery down again."""
    result = pixhost.upload(published_set)
    try:
        _check_pixhost(published_set, result)
    finally:
        removed = _delete_pixhost(result)

    assert removed, (
        "the gallery could not be removed, so this run left a permanent public "
        "gallery on a free service"
    )


def _check_pixhost(published_set, result) -> None:
    assert result.url
    assert len(result.thumbnail_urls) == published_set.total_images
    assert len(result.page_urls) == published_set.total_images
    # A page is not a picture, so nothing must have put one in image_urls.
    assert result.image_urls == ()
    _assert_each_address_holds_its_own_picture(published_set, result.thumbnail_urls)

    requests = _requests()
    page = requests.get(result.page_urls[0], timeout=60)
    assert page.status_code == 200
    assert page.headers.get("Content-Type", "").startswith("text/html"), (
        "show_url stopped being a page; image_urls could now be filled in"
    )


def test_slowpics_round_trip(published_set):
    """Unlisted and short-lived, because this is a test and not a publication."""
    result = slowpics.upload(published_set, remove_after_days=1)

    assert result.url.startswith("https://slow.pics/c/")
    assert result.uploaded + result.skipped == published_set.total_images

    requests = _requests()
    page = requests.get(result.url, timeout=60)
    assert page.status_code == 200


def test_comppics_round_trip(published_set):
    result = comppics.upload(published_set, expiration_days=1)

    assert result.url
    assert len(result.image_urls) == published_set.total_images
    # This host does serve the pictures directly, so the same check applies to
    # image_urls -- and here it is the transpose it is looking for.
    _assert_each_address_holds_its_own_picture(published_set, result.image_urls)
