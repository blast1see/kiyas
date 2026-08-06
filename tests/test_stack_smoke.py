"""Proof that the installed stack can actually do the job.

`kiyas doctor` reports that VapourSynth imports and its plugins are registered.
That is not the same as the pipeline working: an indexer can fail to open a
real container, vs-placebo can be present but refuse a colourspace, and fpng
can be loaded and still write nothing. Every one of those failures looks
healthy to `doctor`.

So this file builds a real HDR10 clip with ffmpeg and pushes it through the
exact chain phase 1 will use -- index, tonemap, write PNG -- and asserts on the
output. It needs both ffmpeg and a working VapourSynth, hence both markers.
"""

from __future__ import annotations

import subprocess

import pytest

from kiyas.media import binaries

pytestmark = [pytest.mark.integration, pytest.mark.vapoursynth]

# Frame property values kiyas keys HDR detection off, straight from the
# VapourSynth constants: BT.2020 primaries, SMPTE ST 2084 (PQ) transfer,
# BT.2020 non-constant luminance matrix.
BT2020_PRIMARIES = 9
PQ_TRANSFER = 16
BT2020_NCL_MATRIX = 9


def _require_tools():
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    try:
        import vapoursynth  # noqa: F401
    except ImportError:
        pytest.skip("VapourSynth is not installed")


@pytest.fixture(scope="module")
def hdr_clip(tmp_path_factory):
    """A short synthetic HDR10 clip.

    Generated rather than committed: a binary fixture in the repository would
    have to be licence-checked, would bloat clones, and would drift from
    whatever ffmpeg actually produces on the machine under test.
    """
    _require_tools()
    ffmpeg = binaries.require_binary("ffmpeg")
    path = tmp_path_factory.mktemp("media") / "hdr10.mkv"

    result = subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=1",
            "-vf",
            "format=yuv420p10le,setparams=color_primaries=bt2020"
            ":color_trc=smpte2084:colorspace=bt2020nc",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:log-level=none",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0 or not path.exists():
        pytest.skip(f"could not build an HDR10 test clip: {result.stderr.strip()[:300]}")
    return path


@pytest.fixture(scope="module")
def indexed(hdr_clip):
    import vapoursynth as vs

    clip = vs.core.lsmas.LWLibavSource(str(hdr_clip))
    return clip


def test_indexer_opens_the_clip(indexed):
    assert indexed.num_frames > 0
    assert indexed.width == 320
    assert indexed.height == 180


def test_hdr_frame_properties_survive_indexing(indexed):
    """HDR detection is driven by these three props.

    If the indexer drops them, kiyas silently treats an HDR source as SDR and
    the screenshots come out washed out -- the exact failure the tonemapping
    exists to prevent.
    """
    props = indexed.get_frame(0).props

    assert props["_Primaries"] == BT2020_PRIMARIES
    assert props["_Transfer"] == PQ_TRANSFER
    assert props["_Matrix"] == BT2020_NCL_MATRIX


def test_placebo_tonemaps_hdr_to_sdr(indexed):
    import numpy as np
    import vapoursynth as vs

    core = vs.core
    source = indexed.resize.Bicubic(format=vs.YUV444P16)
    tonemapped = core.placebo.Tonemap(
        source,
        src_csp=1,  # PlaceboColorSpace.HDR10
        dst_csp=0,  # PlaceboColorSpace.SDR
        gamut_mapping=1,  # Perceptual
        tone_mapping_function=8,  # Mobius
        metadata=2,  # HDR10
        dynamic_peak_detection=0,
        use_dovi=False,
        contrast_recovery=0.0,
    )

    assert tonemapped.num_frames == source.num_frames

    before = np.asarray(source.get_frame(0)[0])
    after = np.asarray(tonemapped.get_frame(0)[0])

    # Tonemapping that changes nothing means the filter silently no-opped.
    assert not np.array_equal(before, after)


def test_screengen_writes_a_png(indexed, tmp_path):
    """awsmfunc.ScreenGen is the writer phase 1 uses; fpng is its backend."""
    import awsmfunc as awf
    import vapoursynth as vs

    clip = indexed.resize.Bicubic(format=vs.RGB24, matrix_in_s="2020ncl")
    out = tmp_path / "screens"

    awf.ScreenGen(clip, folder=out, suffix="-test", frame_numbers=[0, 5])

    written = sorted(out.glob("*.png"))
    assert len(written) == 2
    # A zero-byte file means fpng loaded but never flushed.
    assert all(p.stat().st_size > 0 for p in written)


def test_frameinfo_overlays_a_title(indexed):
    """Source labels are burned into the frame; subtext must be functional."""
    import awsmfunc as awf
    import numpy as np
    import vapoursynth as vs

    clip = indexed.resize.Bicubic(format=vs.YUV420P8, matrix_in_s="2020ncl")
    labelled = awf.FrameInfo(clip, "kiyas smoke test")

    plain = np.asarray(clip.get_frame(0)[0])
    marked = np.asarray(labelled.get_frame(0)[0])

    assert not np.array_equal(plain, marked)
