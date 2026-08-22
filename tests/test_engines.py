"""Engine tests.

The unit tests here cover the decisions an engine makes before it touches a
file. The behaviour that actually matters -- does the right frame come out,
does tonemapping change the picture -- needs real decoding and lives in the
integration tests at the bottom.
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from kiyas import engines
from kiyas.config import Source, Tonemap
from kiyas.engines import EngineError
from kiyas.engines import ffmpeg as ffmpeg_engine_module
from kiyas.engines.base import DARK_LUMA_THRESHOLD
from kiyas.engines.ffmpeg import FfmpegEngine
from kiyas.media import binaries
from kiyas.media.probe import HdrFormat, VideoInfo
from kiyas.run import safe_directory_name


def _info(**overrides) -> VideoInfo:
    defaults = dict(
        path=Path("x.mkv"),
        width=3840,
        height=2160,
        fps=Fraction(24000, 1001),
        duration=100.0,
        frame_count=2397,
        exact=False,
        codec="hevc",
        pix_fmt="yuv420p10le",
        bit_depth=10,
        color_primaries="bt2020",
        color_transfer="smpte2084",
        color_space="bt2020nc",
        dovi_profile=None,
    )
    return VideoInfo(**{**defaults, **overrides})


def _source(**overrides) -> Source:
    source = Source(path=Path("x.mkv"), name="Test")
    for key, value in overrides.items():
        setattr(source, key, value)
    return source


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_unknown_engine_is_rejected():
    with pytest.raises(EngineError, match="unknown engine"):
        engines.get_engine("avisynth")


def test_available_engines_are_ordered_best_first():
    """ffmpeg must never outrank VapourSynth when both work."""
    usable = engines.available_engines()

    if "vapoursynth" in usable and "ffmpeg" in usable:
        assert usable.index("vapoursynth") < usable.index("ffmpeg")


def test_at_least_one_engine_is_available():
    assert engines.available_engines()


# --------------------------------------------------------------------------
# Tonemap mode resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hdr", "expected"),
    [
        (HdrFormat.SDR, Tonemap.NONE),
        (HdrFormat.HDR10, Tonemap.HDR10),
        (HdrFormat.HLG, Tonemap.HDR10),
        (HdrFormat.DOVI, Tonemap.DOVI),
    ],
)
def test_vapoursynth_auto_tonemap_follows_the_source(hdr, expected):
    pytest.importorskip("vapoursynth")
    from kiyas.engines.vapoursynth import VapourSynthEngine

    info = _info(dovi_profile=5 if hdr is HdrFormat.DOVI else None)
    if hdr is HdrFormat.SDR:
        info = _info(color_primaries="bt709", color_transfer="bt709")
    elif hdr is HdrFormat.HLG:
        info = _info(color_transfer="arib-std-b67")

    assert VapourSynthEngine()._tonemap_mode(_source(), info) is expected


def test_explicit_tonemap_overrides_detection():
    """The user has seen the file; the container tag may be wrong."""
    pytest.importorskip("vapoursynth")
    from kiyas.engines.vapoursynth import VapourSynthEngine

    source = _source(tonemap=Tonemap.NONE)

    assert VapourSynthEngine()._tonemap_mode(source, _info()) is Tonemap.NONE


def test_ffmpeg_maps_dolby_vision_down_to_hdr10():
    """The ffmpeg engine has no DoVi support, so auto must not select it."""
    assert FfmpegEngine()._tonemap_mode(_source(), _info(dovi_profile=8)) is Tonemap.HDR10


def test_missing_file_is_reported_by_name():
    engine = FfmpegEngine()

    with pytest.raises(EngineError, match="no such file"):
        engine.prepare(_source(path=Path("does-not-exist.mkv"), name="WEB-DL"))


# --------------------------------------------------------------------------
# ffmpeg frame addressing
# --------------------------------------------------------------------------


def _ffmpeg_source(**overrides):
    from kiyas.engines.ffmpeg import FfmpegSource

    return FfmpegSource(
        _source(**overrides),
        _info(fps=Fraction(24), frame_count=1000),
        Path("ffmpeg"),
        Path("ffprobe"),
        [],
        None,
    )


def test_timestamp_targets_the_middle_of_the_frame():
    """Landing on the boundary makes frame choice a rounding question."""
    prepared = _ffmpeg_source()

    assert prepared._timestamp(0) == pytest.approx(0.5 / 24)
    assert prepared._timestamp(100) == pytest.approx(100.5 / 24)


def _expected_margin(total: int) -> int:
    return max(
        ffmpeg_engine_module._ESTIMATE_MARGIN_MIN,
        int(total * ffmpeg_engine_module._ESTIMATE_MARGIN),
    )


def test_trim_shifts_the_timestamp_not_the_index():
    """Frame 0 of a trimmed source is frame `trim` of the file.

    This has to match VapourSynth's behaviour exactly, or the same project
    file produces different screenshots depending on the engine.
    """
    prepared = _ffmpeg_source(trim=48)

    assert prepared._timestamp(0) == pytest.approx(48.5 / 24)
    assert prepared.frame_count == 1000 - _expected_margin(1000) - 48


def test_frame_count_accounts_for_trim():
    assert _ffmpeg_source(trim=100).frame_count == 1000 - _expected_margin(1000) - 100


def _source_with(total: int, *, exact: bool):
    from kiyas.engines.ffmpeg import FfmpegSource

    return FfmpegSource(
        _source(), _info(fps=Fraction(24), frame_count=total, exact=exact),
        Path("ffmpeg"), Path("ffprobe"), [], None,
    )  # fmt: skip


def test_estimated_frame_counts_lose_a_tail_margin():
    """A container's frame count overshoots, and seeking past EOF just fails.

    Measured on a retail remux: ffprobe said 157138 where the real count was
    157013. Without the margin the last screenshot of a run dies with an
    unexplained ffmpeg error while every earlier one worked.
    """
    assert _source_with(1000, exact=False).frame_count < 1000
    assert _source_with(1000, exact=True).frame_count == 1000, "an exact count needs no margin"


def test_the_margin_covers_the_error_actually_measured():
    """157013 real against 157138 estimated: the margin has to absorb 125."""
    assert _source_with(157138, exact=False).frame_count <= 157013


def test_the_margin_does_not_erase_short_clips():
    """A fixed ten-second margin was tried first and zeroed four-second clips.

    Any absolute threshold breaks at one end of the scale or the other; this
    is the second place in kiyas where that had to be learned.
    """
    ninety_six_frames = _source_with(96, exact=False)

    assert ninety_six_frames.frame_count > 90


@pytest.mark.parametrize("total", [10, 96, 1000, 157138, 500_000])
def test_margin_never_produces_a_negative_or_zero_length(total):
    assert _source_with(total, exact=False).frame_count > 0


def test_ffmpeg_admits_when_it_cannot_read_picture_types(monkeypatch):
    """Silence here used to look like "every frame is an I-frame".

    A failed ffprobe scan cached nothing, so picture_type answered None for
    everything, the selector rejected every candidate, and the user got "no
    usable frames were found" with no explanation. Now the engine says it
    cannot tell, and the orchestrator warns instead.
    """
    prepared = _ffmpeg_source()
    monkeypatch.setattr(prepared, "_scan_pict_types", lambda *_a, **_k: None)

    assert prepared.supports_frame_types is False
    assert prepared.picture_type(10) is None


def test_ffmpeg_reports_picture_types_when_the_scan_works(monkeypatch):
    prepared = _ffmpeg_source()

    def fake_scan(frame, span_seconds=4.0):
        prepared._pict_cache.update({frame: "B", frame + 1: "I"})

    monkeypatch.setattr(prepared, "_scan_pict_types", fake_scan)

    assert prepared.supports_frame_types is True


def test_ffmpeg_does_not_mark_an_empty_window_as_scanned(monkeypatch):
    """Recording an empty scan would make the window permanently non-B."""
    prepared = _ffmpeg_source()

    class _Empty:
        stdout = ""
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *_a, **_k: _Empty())
    prepared._scan_pict_types(100)

    assert prepared._scanned == []


def test_dark_threshold_is_above_limited_range_black():
    """Limited-range black sits at 16/255 = 0.063.

    A threshold below that would never fire; far above it would reject the
    dark night scenes that are the most useful thing to compare.
    """
    assert 16 / 255 < DARK_LUMA_THRESHOLD < 0.2


# --------------------------------------------------------------------------
# Integration: real files
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """An SDR clip and an HDR10 clip of the same synthetic content."""
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    ffmpeg = binaries.require_binary("ffmpeg")
    directory = tmp_path_factory.mktemp("engine-media")

    sdr = directory / "sdr.mkv"
    subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "12", "-bf", "3",
            "-pix_fmt", "yuv420p", str(sdr),
        ],  # fmt: skip
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip

    hdr = directory / "hdr.mkv"
    subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=4",
            "-vf", "format=yuv420p10le,setparams=color_primaries=bt2020"
                   ":color_trc=smpte2084:colorspace=bt2020nc",
            "-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:log-level=none",
            str(hdr),
        ],  # fmt: skip
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip

    return {"sdr": sdr, "hdr": hdr}


@pytest.fixture(params=["vapoursynth", "ffmpeg"])
def engine(request):
    if request.param == "vapoursynth":
        pytest.importorskip("vapoursynth")
    instance = engines.get_engine(request.param)
    if not instance.available():
        pytest.skip(f"{request.param} is not available")
    return instance


@pytest.mark.integration
def test_engines_agree_on_frame_count(engine, clips):
    """Both engines must see the same clip length.

    If they disagree, the same project file selects different frames depending
    on which engine ran -- and the comparison is not reproducible.
    """
    prepared = engine.prepare(_source(path=clips["sdr"], name="SDR"))

    assert prepared.frame_count == pytest.approx(96, abs=2)


@pytest.mark.integration
def test_engine_writes_requested_frames(engine, clips, tmp_path):
    prepared = engine.prepare(_source(path=clips["sdr"], name="SDR"))

    written = prepared.write_frames([10, 40], tmp_path / "shots")

    assert [p.name for p in written] == ["000010.png", "000040.png"]
    assert all(p.stat().st_size > 0 for p in written)


@pytest.mark.integration
def test_crop_reaches_the_output(engine, clips, tmp_path):
    from PIL import Image

    source = _source(path=clips["sdr"], name="SDR", crop=(20, 20, 30, 30))
    prepared = engine.prepare(source)

    written = prepared.write_frames([10], tmp_path / "cropped")

    with Image.open(written[0]) as image:
        assert image.size == (640 - 40, 360 - 60)


@pytest.mark.integration
def test_resize_reaches_the_output(engine, clips, tmp_path):
    from PIL import Image

    prepared = engine.prepare(_source(path=clips["sdr"], name="SDR", resize=(320, 180)))

    written = prepared.write_frames([10], tmp_path / "resized")

    with Image.open(written[0]) as image:
        assert image.size == (320, 180)


def _has_filter(name: str) -> bool:
    """Whether the installed ffmpeg has a filter.

    Distribution builds vary: the tonemap chain needs zscale, and plenty of
    packaged ffmpegs are built without libzimg. `doctor` reports that as a
    partial engine rather than a failure, and a test asserting on a filter
    that is not there should say the same thing rather than fail.
    """
    proc = subprocess.run(  # noqa: S603
        [str(binaries.require_binary("ffmpeg")), "-hide_banner", "-filters"],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    return any(line.split()[1:2] == [name] for line in proc.stdout.splitlines() if line.split())


def _pixels(path: Path) -> bytes:
    from PIL import Image

    with Image.open(path) as image:
        return image.convert("RGB").tobytes()


@pytest.mark.integration
def test_hdr_source_is_tonemapped(engine, clips, tmp_path):
    """Without tonemapping an HDR capture comes out washed out and flat.

    overlay=False is not cosmetic here. FrameInfo burns the source name and
    the frame number into the picture, so two captures with different labels
    differ in the top-left corner no matter what the tonemapper did -- and an
    earlier version of this test passed for exactly that reason while proving
    nothing.
    """
    if engine.name == "ffmpeg" and not _has_filter("zscale"):
        pytest.skip("this ffmpeg was built without zscale; doctor reports that too")

    plain = engine.prepare(
        _source(path=clips["hdr"], name="x", tonemap=Tonemap.NONE), overlay=False
    )
    mapped = engine.prepare(
        _source(path=clips["hdr"], name="x", tonemap=Tonemap.HDR10), overlay=False
    )

    a = plain.write_frames([12], tmp_path / "plain")[0]
    b = mapped.write_frames([12], tmp_path / "mapped")[0]

    assert _pixels(a) != _pixels(b)


@pytest.mark.integration
def test_hdr10_plus_is_a_different_curve_and_only_that(clips, tmp_path):
    """What the hdr10plus option still buys, now that we know what it does not.

    It selects the ST2094-40 tone curve. It does *not* apply HDR10+ per-scene
    metadata -- measured against vs-placebo 2.0.4, where choosing HDR10+ as the
    metadata source and stripping the HDR10Plus frame property both leave the
    output bit-identical.

    So the curve is the whole of it, and this test holds that: merge the two
    branches as a "simplification" and every hdr10plus comparison silently
    becomes an hdr10 one. A future vs-placebo that honours the metadata would
    not fail this test -- it would need a clip carrying HDR10+, which the
    fixtures here cannot make.
    """
    from kiyas.engines.vapoursynth import VapourSynthEngine

    engine = VapourSynthEngine()
    if not engine.available():
        pytest.skip("the VapourSynth stack is not installed")

    static = engine.prepare(
        _source(path=clips["hdr"], name="x", tonemap=Tonemap.HDR10), overlay=False
    )
    curve = engine.prepare(
        _source(path=clips["hdr"], name="x", tonemap=Tonemap.HDR10PLUS), overlay=False
    )

    a = static.write_frames([12], tmp_path / "static")[0]
    b = curve.write_frames([12], tmp_path / "curve")[0]

    assert _pixels(a) != _pixels(b), "hdr10plus collapsed into hdr10"


@pytest.mark.integration
def test_trim_shifts_which_frame_is_captured(engine, clips, tmp_path):
    """Frame 10 of a clip trimmed by 10 must be frame 20 of the original.

    Same reason for overlay=False: the burnt-in frame number differs between
    the two captures even when the underlying picture is identical.
    """
    untrimmed = engine.prepare(_source(path=clips["sdr"], name="x"), overlay=False)
    trimmed = engine.prepare(_source(path=clips["sdr"], name="x", trim=10), overlay=False)

    a = untrimmed.write_frames([20], tmp_path / "a")[0]
    b = trimmed.write_frames([10], tmp_path / "b")[0]

    assert _pixels(a) == _pixels(b)


@pytest.mark.integration
@pytest.mark.vapoursynth
def test_overlay_changes_the_picture_and_can_be_turned_off(clips, tmp_path):
    """The label is burnt in, so it has to be optional and it has to be off
    whenever pixels are being compared."""
    pytest.importorskip("vapoursynth")
    engine = engines.get_engine("vapoursynth")
    if not engine.available():
        pytest.skip("VapourSynth is not available")

    with_label = engine.prepare(_source(path=clips["sdr"], name="LABEL"), overlay=True)
    without = engine.prepare(_source(path=clips["sdr"], name="LABEL"), overlay=False)

    a = with_label.write_frames([12], tmp_path / "with")[0]
    b = without.write_frames([12], tmp_path / "without")[0]

    assert _pixels(a) != _pixels(b)


@pytest.mark.integration
def test_ffmpeg_refuses_explicit_dolby_vision_with_a_way_out(clips):
    """Silently tonemapping DV as HDR10 when asked for DV would be a lie."""
    engine = FfmpegEngine()
    source = _source(path=clips["hdr"], name="DV", tonemap=Tonemap.DOVI)

    with pytest.raises(EngineError, match="VapourSynth engine"):
        engine.prepare(source)


@pytest.mark.integration
def test_ffmpeg_refuses_hdr10_plus(clips):
    engine = FfmpegEngine()
    source = _source(path=clips["hdr"], name="HDR10+", tonemap=Tonemap.HDR10PLUS)

    with pytest.raises(EngineError, match="HDR10\\+"):
        engine.prepare(source)


@pytest.mark.integration
def test_engines_report_whether_they_labelled_the_frame(engine, clips):
    """A comparison with half its sources labelled is worse than none.

    Both engines label now. This used to assert that only VapourSynth did,
    because ffmpeg's drawtext needs a font by path and there is no portable
    one; kiyas ships a font, so the gap is closed rather than the rule
    relaxed. It can still come back false -- an install missing the font, or
    an ffmpeg built without drawtext -- which is what the property is for.
    """
    if engine.name == "ffmpeg" and not _has_filter("drawtext"):
        pytest.skip("this ffmpeg was built without drawtext; doctor reports that too")

    prepared = engine.prepare(_source(path=clips["sdr"], name="SDR"), overlay=True)

    assert prepared.has_overlay is True


@pytest.mark.integration
def test_a_name_full_of_filter_metacharacters_still_renders(engine, clips, tmp_path):
    """Release names are free text and ffmpeg's filtergraph is not.

    Every character in this name breaks a different one of the three parsers
    a drawtext option passes through, and the apostrophe combined with the
    colon in "Picture type:" has no working escape at all -- which is why the
    text travels in a file instead. If that ever regresses, ffmpeg fails
    outright rather than drawing something slightly wrong, so a rendered frame
    is the whole assertion.
    """
    if engine.name == "ffmpeg" and not _has_filter("drawtext"):
        pytest.skip("this ffmpeg was built without drawtext; doctor reports that too")

    name = "Director's Cut 100% [a, b]; c \\ d (DV: FEL)"
    prepared = engine.prepare(_source(path=clips["sdr"], name=name), overlay=True)

    written = prepared.write_frames([5], tmp_path / "shots")

    assert len(written) == 1
    assert written[0].stat().st_size > 0


@pytest.mark.integration
def test_a_label_survives_an_output_directory_named_after_the_source(engine, clips, tmp_path):
    """`run` names the output directory after the source, and names are free text.

    This is the same escaping problem as the label text, arriving through the
    *path* instead: an apostrophe, a comma or a bracket in a filter option
    value breaks the filtergraph, and "Director's Cut" is an ordinary folder
    to end up with. It failed with "Error opening output files", which points
    at the output and is not where the problem was.
    """
    if engine.name == "ffmpeg" and not _has_filter("drawtext"):
        pytest.skip("this ffmpeg was built without drawtext; doctor reports that too")

    name = "Director's Cut, take [2] 100%"
    prepared = engine.prepare(_source(path=clips["sdr"], name=name), overlay=True)

    written = prepared.write_frames([5], tmp_path / safe_directory_name(name))

    assert len(written) == 1
    assert written[0].stat().st_size > 0


@pytest.mark.integration
def test_out_of_range_frame_is_refused(engine, clips, tmp_path):
    prepared = engine.prepare(_source(path=clips["sdr"], name="SDR"))

    with pytest.raises(EngineError, match="outside the clip"):
        prepared.write_frames([10_000], tmp_path / "nope")


@pytest.mark.integration
def test_picture_types_are_readable(engine, clips):
    """B-frame selection is the point; an engine claiming support must deliver."""
    prepared = engine.prepare(_source(path=clips["sdr"], name="SDR"))
    if not prepared.supports_frame_types:
        pytest.skip(f"{engine.name} does not report picture types")

    found = [n for n in range(2, 40) if prepared.picture_type(n) == "B"]

    assert found, "no B-frames found in a clip encoded with -bf 3"


@pytest.mark.integration
def test_mean_luma_is_a_fraction(engine, clips):
    prepared = engine.prepare(_source(path=clips["sdr"], name="SDR"))

    luma = prepared.mean_luma(10)

    assert 0.0 <= luma <= 1.0
    # testsrc2 is a bright test pattern, not black.
    assert luma > DARK_LUMA_THRESHOLD


# --------------------------------------------------------------------------
# The enhancement layer, from an engine that cannot compose one
# --------------------------------------------------------------------------


def _dual_layer(**overrides):
    return _info(dovi_profile=7, dovi_el_present=True, **overrides)


def test_ffmpeg_says_it_left_the_enhancement_layer_out():
    """Silence here is the exact failure composing it was written to end.

    A profile 7 release carries half its picture in a second layer. Producing
    base-layer screenshots and offering them as screenshots of the release is
    what kiyas did for as long as dovi_tool sat unused, and an engine that
    cannot compose the layer has to say so rather than repeat it quietly.
    """
    note = FfmpegEngine()._enhancement_note(_source(), _dual_layer())

    assert note is not None
    assert "base layer alone" in note
    assert "VapourSynth" in note


def test_ffmpeg_refuses_an_explicit_request_it_cannot_honour():
    """`on` asks for a specific picture, so quietly producing another is worse
    than refusing to produce one."""
    from kiyas.config import DoviEl

    with pytest.raises(EngineError, match="only the VapourSynth engine"):
        FfmpegEngine()._enhancement_note(_source(dovi_el=DoviEl.ON), _dual_layer())


@pytest.mark.parametrize("info", [_info(dovi_profile=8), _info(dovi_profile=None)])
def test_a_single_layer_source_is_not_mentioned_at_all(info):
    """Profile 8 has nothing to compose, so a caveat there is noise."""
    assert FfmpegEngine()._enhancement_note(_source(), info) is None


def test_switching_it_off_is_silence_not_a_warning():
    """'off' means never bring it up again, not merely never do it."""
    from kiyas.config import DoviEl

    assert FfmpegEngine()._enhancement_note(_source(dovi_el=DoviEl.OFF), _dual_layer()) is None
