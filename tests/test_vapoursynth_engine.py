"""VapourSynth-engine specifics that do not fit the shared engine tests."""

from __future__ import annotations

import subprocess

import pytest

from kiyas.media import binaries

pytest.importorskip("vapoursynth")

from kiyas.engines import vapoursynth as vs_engine  # noqa: E402

pytestmark = pytest.mark.vapoursynth


# --------------------------------------------------------------------------
# Indexer output handling
# --------------------------------------------------------------------------


def test_progress_lines_are_recognised():
    """The exact wording L-SMASH uses. If this changes, the spam comes back."""
    assert vs_engine._INDEX_PROGRESS.search("Creating lwi index file 42%")
    assert vs_engine._INDEX_PROGRESS.search("Creating lwi index file 100%")
    assert vs_engine._INDEX_PROGRESS.search("Creating lwi index file 42%").group(1) == "42"


def test_real_complaints_are_not_mistaken_for_progress():
    assert not vs_engine._INDEX_PROGRESS.search("unsupported codec in stream 0")


def test_vapoursynths_own_chatter_is_filtered():
    """Otherwise every run reports 'indexer said: New message handler installed'.

    VapourSynth announces its log handler on stderr, which the fd-level capture
    picks up along with everything else.
    """
    assert vs_engine._BENIGN.search("New message handler installed from python")
    assert not vs_engine._BENIGN.search("Broken index file, reindexing")


def test_capture_restores_stderr_and_collects_nothing_when_quiet():
    import os
    import sys

    before = os.dup(2)
    try:
        with vs_engine._capture_indexing(lambda _t: None, "x") as leftovers:
            pass
        assert leftovers == []
        # stderr must still be usable afterwards.
        sys.stderr.write("")
        sys.stderr.flush()
    finally:
        os.close(before)


def test_capture_reports_progress_through_the_callback():
    seen: list[str] = []

    with vs_engine._capture_indexing(seen.append, "SOURCE"):
        import vapoursynth as vs

        vs.core.log_message(vs.MESSAGE_TYPE_INFORMATION, "Creating lwi index file 55%")

    assert any("indexing SOURCE 55%" in line for line in seen)


def test_capture_keeps_real_warnings():
    with vs_engine._capture_indexing(lambda _t: None, "SOURCE") as leftovers:
        import vapoursynth as vs

        vs.core.log_message(vs.MESSAGE_TYPE_WARNING, "something is actually wrong")

    assert any("actually wrong" in line for line in leftovers)


# --------------------------------------------------------------------------
# Brightness measurement
# --------------------------------------------------------------------------


def test_active_area_crops_the_frame():
    import vapoursynth as vs

    clip = vs.core.std.BlankClip(width=1920, height=1080, format=vs.YUV420P8, length=1)

    cropped = vs_engine._active_area(clip)

    assert cropped.width < clip.width
    assert cropped.height < clip.height
    assert cropped.width == pytest.approx(1920 * vs_engine.ACTIVE_AREA, abs=4)


def test_active_area_stays_valid_on_subsampled_formats():
    """CropRel refuses odd offsets on 4:2:0; a rounding slip raises at runtime."""
    import vapoursynth as vs

    for width, height in ((1920, 1080), (3840, 2160), (720, 480), (1280, 720)):
        clip = vs.core.std.BlankClip(width=width, height=height, format=vs.YUV420P8, length=1)
        cropped = vs_engine._active_area(clip)
        cropped.get_frame(0)  # must not raise


def test_active_area_leaves_tiny_clips_alone():
    import vapoursynth as vs

    clip = vs.core.std.BlankClip(width=4, height=4, format=vs.YUV420P8, length=1)

    assert vs_engine._active_area(clip).width == 4


@pytest.mark.integration
def test_letterbox_bars_do_not_make_a_bright_frame_look_dark(tmp_path):
    """The bug this guards against was found on a real 2.39:1 remux.

    A well-lit frame measured below the dark threshold purely because a quarter
    of the picture was black bars, so the frame was thrown away.
    """
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    from pathlib import Path

    from kiyas.config import Source
    from kiyas.engines.base import DARK_LUMA_THRESHOLD

    ffmpeg = binaries.require_binary("ffmpeg")
    path = tmp_path / "scope.mkv"
    # A mid-grey 2.39:1 image letterboxed into 16:9: bright picture, black bars.
    subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=gray:s=1920x804:r=24:d=2",
            "-vf", "pad=1920:1080:0:138:black",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],  # fmt: skip
        check=True, capture_output=True, timeout=180,
    )  # fmt: skip

    engine = vs_engine.VapourSynthEngine()
    if not engine.available():
        pytest.skip("VapourSynth is not available")
    prepared = engine.prepare(Source(path=Path(path), name="scope"), overlay=False)
    try:
        assert prepared.mean_luma(10) > DARK_LUMA_THRESHOLD
    finally:
        prepared.close()
