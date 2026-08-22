from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from kiyas.media import binaries, probe
from kiyas.media.probe import HdrFormat, ProbeError, VideoInfo


def _info(**overrides) -> VideoInfo:
    defaults = dict(
        path=Path("x.mkv"),
        width=1920,
        height=1080,
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


# --------------------------------------------------------------------------
# HDR classification
# --------------------------------------------------------------------------


def test_pq_transfer_is_hdr10():
    assert _info().hdr_format is HdrFormat.HDR10


def test_alternate_pq_spelling_is_recognised():
    """ffmpeg emits both spellings depending on how the file was tagged."""
    assert _info(color_transfer="smpte-st-2084").hdr_format is HdrFormat.HDR10


def test_hlg_is_detected():
    assert _info(color_transfer="arib-std-b67").hdr_format is HdrFormat.HLG


def test_dolby_vision_wins_over_the_transfer():
    """A DoVi profile 5 file has no usable HDR10 layer.

    Tonemapping one as HDR10 produces the green/purple cast that makes those
    screenshots worthless, so the DoVi record has to take priority over
    whatever the transfer characteristics claim.
    """
    assert _info(dovi_profile=5, color_transfer="smpte2084").hdr_format is HdrFormat.DOVI


def test_bt2020_without_pq_is_not_hdr():
    """Wide-gamut SDR exists. Tonemapping it would crush a correct picture."""
    info = _info(color_primaries="bt2020", color_transfer="bt709")

    assert info.hdr_format is HdrFormat.SDR
    assert info.is_wide_gamut is True


def test_plain_bt709_is_sdr():
    info = _info(color_primaries="bt709", color_transfer="bt709", color_space="bt709")

    assert info.hdr_format is HdrFormat.SDR
    assert info.is_wide_gamut is False
    assert info.hdr_format.is_hdr is False


def test_missing_colour_tags_default_to_sdr():
    assert _info(color_primaries=None, color_transfer=None).hdr_format is HdrFormat.SDR


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def test_frame_rate_stays_a_fraction():
    """23.976 is not 24000/1001.

    Rounding here and multiplying by a two-hour runtime drifts by several
    frames -- the same order as the desync a comparison is meant to expose.
    """
    fps = probe._parse_fraction("24000/1001", Fraction(25))

    assert fps == Fraction(24000, 1001)
    assert fps != Fraction(23976, 1000)


def test_zero_frame_rate_falls_back_instead_of_dividing_by_zero():
    """ffprobe reports 0/0 for streams whose rate it cannot determine."""
    assert probe._parse_fraction("0/0", Fraction(24)) == Fraction(24)


def test_unparseable_frame_rate_falls_back():
    assert probe._parse_fraction("N/A", Fraction(24)) == Fraction(24)
    assert probe._parse_fraction(None, Fraction(24)) == Fraction(24)


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ({"bits_per_raw_sample": "10"}, 10),
        ({"pix_fmt": "yuv420p10le"}, 10),
        ({"pix_fmt": "yuv422p12le"}, 12),
        ({"pix_fmt": "yuv420p"}, 8),
        ({}, 8),
        ({"bits_per_raw_sample": "N/A", "pix_fmt": "yuv420p10le"}, 10),
    ],
)
def test_bit_depth_falls_back_to_the_pixel_format(stream, expected):
    assert probe._bit_depth(stream) == expected


def test_dovi_profile_read_from_side_data():
    stream = {"side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8}]}

    assert probe._dovi_profile(stream) == 8


def test_no_side_data_means_no_dovi():
    assert probe._dovi_profile({}) is None
    assert probe._dovi_profile({"side_data_list": []}) is None


# --------------------------------------------------------------------------
# Dolby Vision enhancement layers
#
# Both payloads below were copied out of ffprobe rather than written by hand,
# from two releases of the same 2026 film: a UHD disc remux and an iTunes
# WEB-DL hybrid. A configuration record invented from the documentation would
# agree with whatever this module happened to do.
# --------------------------------------------------------------------------

PROFILE_7_REMUX = {
    "side_data_list": [
        {
            "side_data_type": "DOVI configuration record",
            "dv_version_major": 1,
            "dv_version_minor": 0,
            "dv_profile": 7,
            "dv_level": 6,
            "rpu_present_flag": 1,
            "el_present_flag": 1,
            "bl_present_flag": 1,
            "dv_bl_signal_compatibility_id": 6,
            "dv_md_compression": "none",
        }
    ]
}

PROFILE_8_WEB_DL = {
    "side_data_list": [
        {
            "side_data_type": "DOVI configuration record",
            "dv_version_major": 1,
            "dv_version_minor": 0,
            "dv_profile": 8,
            "dv_level": 6,
            "rpu_present_flag": 1,
            "el_present_flag": 0,
            "bl_present_flag": 1,
            "dv_bl_signal_compatibility_id": 1,
            "dv_md_compression": "none",
        }
    ]
}


def test_a_profile_7_remux_reports_an_enhancement_layer():
    assert probe._dovi_el_present(PROFILE_7_REMUX) is True


def test_a_profile_8_hybrid_reports_no_enhancement_layer():
    """The flag is what separates the two, not the profile on its own."""
    assert probe._dovi_el_present(PROFILE_8_WEB_DL) is False


def test_a_file_with_no_dolby_vision_has_no_enhancement_layer():
    assert probe._dovi_el_present({}) is False
    assert probe._dovi_el_present({"side_data_list": []}) is False


def test_only_profile_7_counts_as_dual_layer():
    """Profile 8 with a stray el_present_flag is still a single-layer file.

    Asking for both keeps a mis-tagged file from sending the engine off to
    demux a layer that is not there, which costs a full read of the source
    before it can fail.
    """
    assert _info(dovi_profile=7, dovi_el_present=True).has_enhancement_layer is True
    assert _info(dovi_profile=8, dovi_el_present=True).has_enhancement_layer is False
    assert _info(dovi_profile=7, dovi_el_present=False).has_enhancement_layer is False
    assert _info(dovi_profile=None).has_enhancement_layer is False


def test_missing_file_is_reported_before_spawning_ffprobe(tmp_path):
    with pytest.raises(ProbeError, match="no such file"):
        probe.probe(tmp_path / "nope.mkv")


# --------------------------------------------------------------------------
# Against real files
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sdr_clip(tmp_path_factory):
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    ffmpeg = binaries.require_binary("ffmpeg")
    path = tmp_path_factory.mktemp("probe") / "sdr.mkv"
    subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24000/1001:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )  # fmt: skip
    return path


@pytest.mark.integration
def test_probe_reads_a_real_file(sdr_clip):
    info = probe.probe(sdr_clip)

    assert info.width == 640
    assert info.height == 360
    assert info.fps == Fraction(24000, 1001)
    assert info.codec == "h264"
    assert info.bit_depth == 8
    assert info.hdr_format is HdrFormat.SDR
    assert info.duration == pytest.approx(2.0, abs=0.2)


@pytest.mark.integration
def test_frame_count_is_close_even_when_estimated(sdr_clip):
    """Matroska rarely carries nb_frames, so this is duration * fps."""
    info = probe.probe(sdr_clip)

    assert info.frame_count == pytest.approx(48, abs=2)


@pytest.mark.integration
def test_probing_a_non_media_file_fails_cleanly(tmp_path):
    junk = tmp_path / "not-a-video.mkv"
    junk.write_text("definitely not a matroska file")

    with pytest.raises(ProbeError):
        probe.probe(junk)
