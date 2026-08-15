"""Audio analysis.

The measurements are checked against signals whose answers are known by
construction: a 16-bit file rewrapped as 24-bit really does use sixteen bits, a
sine amplified thirty times really does clip, and a track built by copying one
channel really does have identical channels. Anything measured from real
material can only be checked for plausibility, which is not the same thing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiyas.audio import analysis, table
from kiyas.audio.analysis import AnalysisError, channel_names
from kiyas.audio.probe import AudioTrack, probe_track
from kiyas.audio.sync import Offset
from kiyas.media import binaries

pytestmark = pytest.mark.integration


def _track(**overrides) -> AudioTrack:
    defaults = dict(
        path=Path("x.flac"),
        index=0,
        codec="flac",
        profile=None,
        channels=2,
        channel_layout="stereo",
        sample_rate=48000,
        bit_depth=24,
        bitrate=None,
        duration=10.0,
        language=None,
        title=None,
    )
    return AudioTrack(**{**defaults, **overrides})


# --------------------------------------------------------------------------
# No media needed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channels", "expected"),
    [(1, "Mono"), (2, "L"), (6, "L"), (7, "ch1")],
)
def test_channels_are_named_in_ffmpeg_order(channels, expected):
    assert channel_names(channels)[0] == expected
    assert len(channel_names(channels)) == channels


def test_the_lfe_channel_is_named_in_a_5_1_layout():
    assert channel_names(6) == ["L", "R", "C", "LFE", "Ls", "Rs"]


def test_lossless_codecs_are_recognised():
    assert _track(codec="flac").is_lossless
    assert _track(codec="truehd").is_lossless
    assert not _track(codec="ac3").is_lossless
    assert not _track(codec="eac3").is_lossless


def test_the_offset_summary_states_a_direction():
    assert "later" in Offset(250.0, 100.0, "GCC-PHAT").summary
    assert "earlier" in Offset(-250.0, 100.0, "GCC-PHAT").summary


def test_a_weak_offset_says_so_rather_than_looking_confident():
    weak = Offset(12.0, 1.5, "GCC-PHAT")

    assert weak.is_weak
    assert "weak match" in weak.summary


# --------------------------------------------------------------------------
# Measurements, against signals with known answers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ffmpeg() -> Path:
    if binaries.find_binary("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    return binaries.require_binary("ffmpeg")


@pytest.fixture(scope="module")
def clips(tmp_path_factory, ffmpeg) -> dict[str, Path]:
    """Signals built so that every measurement has a known right answer."""
    directory = tmp_path_factory.mktemp("audio-media")

    def build(name: str, source: list[str], filters: str, extra: list[str]) -> Path:
        path = directory / name
        subprocess.run(  # noqa: S603
            [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", *source]
            + (["-af", filters] if filters else [])
            + [*extra, str(path)],
            check=True,
            capture_output=True,
            timeout=300,
        )
        return path

    sine = ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4"]
    clips = {
        "sixteen": build(
            "sixteen.flac", sine, "volume=0.5", ["-c:a", "flac", "-sample_fmt", "s16"]
        ),
        "clipped": build("clipped.flac", sine, "volume=30", ["-c:a", "flac", "-sample_fmt", "s16"]),
        "dual_mono": build(
            "dual.flac",
            sine,
            "volume=0.4,pan=stereo|c0=c0|c1=c0",
            ["-c:a", "flac", "-sample_fmt", "s16"],
        ),
        "half_silent": build(
            "half.flac",
            sine,
            "volume=0.4,pan=stereo|c0=c0|c1=0*c0",
            ["-c:a", "flac", "-sample_fmt", "s16"],
        ),
        "lossy": build("lossy.ac3", sine, "volume=0.5", ["-c:a", "ac3", "-b:a", "192k"]),
    }
    # A 16-bit file rewrapped as 24-bit: the container claims more than the
    # content has, which is the case the measurement exists for.
    clips["padded"] = build(
        "padded.flac", ["-i", str(clips["sixteen"])], "", ["-c:a", "flac", "-sample_fmt", "s32"]
    )
    return clips


def test_a_sixteen_bit_file_in_a_24_bit_container_is_caught(clips):
    track = probe_track(clips["padded"])
    assert track.bit_depth == 24, "the container should claim 24 bits"

    result = analysis.analyse(track)

    assert result.real_bit_depth == 16
    assert any("padding" in note for note in result.notes)


def test_a_real_sixteen_bit_file_is_not_accused_of_anything(clips):
    result = analysis.analyse(probe_track(clips["sixteen"]))

    assert result.real_bit_depth == 16
    assert not any("padding" in note for note in result.notes)


def test_bit_depth_is_refused_for_a_lossy_codec(clips):
    """A lossy decoder's float output uses every bit whatever the source was."""
    result = analysis.analyse(probe_track(clips["lossy"]))

    assert result.real_bit_depth is None
    assert any("not measurable" in note for note in result.notes)


def test_clipping_is_counted_in_runs(clips):
    result = analysis.analyse(probe_track(clips["clipped"]))

    assert result.overall_peak_dbfs == pytest.approx(0.0, abs=0.01)
    # A 1 kHz sine over four seconds clips twice per cycle once it is driven
    # into the rails: 4000 cycles, 8000 runs, give or take the ends.
    assert result.clipped == pytest.approx(8000, rel=0.02)
    assert result.clip_positions


def test_an_unclipped_track_reports_no_clipping(clips):
    result = analysis.analyse(probe_track(clips["sixteen"]))

    assert result.clipped == 0
    assert result.overall_peak_dbfs < -3


def test_a_silent_channel_is_reported(clips):
    result = analysis.analyse(probe_track(clips["half_silent"]))

    assert result.silent_channels == ["R"]
    assert any("silent channel" in note for note in result.notes)


def test_duplicated_channels_are_reported(clips):
    """A stereo track that is really mono twice, which is what a dub often is."""
    result = analysis.analyse(probe_track(clips["dual_mono"]))

    assert result.duplicate_channels == [("L", "R")]
    assert any("identical channel" in note for note in result.notes)
    assert not result.silent_channels


def test_distinct_channels_are_not_called_duplicates(clips):
    result = analysis.analyse(probe_track(clips["half_silent"]))

    assert result.duplicate_channels == []


def test_the_frequency_response_finds_the_tone(clips):
    """A 1 kHz sine has to come back as 1 kHz, or the axis is wrong."""
    import numpy as np

    result = analysis.analyse(probe_track(clips["sixteen"]))
    loudest = result.frequencies[int(np.argmax(result.spectrum[0]))]

    assert loudest == pytest.approx(1000, abs=30)


def test_the_channels_do_not_get_rotated(clips):
    """The decode is read from a pipe, which hands over partial frames.

    Dropping a partial frame instead of carrying it starts the next read half a
    frame late and swaps the channels from that point on. It cannot happen on a
    mono file, so a test that only used one would never see it -- and it
    produced a spectrum whose loudest component was DC.
    """
    import numpy as np

    result = analysis.analyse(probe_track(clips["half_silent"]))

    assert result.peak_dbfs[0] > -40, "the left channel should carry the tone"
    assert result.peak_dbfs[1] < analysis.SILENT_DBFS, "the right channel should be empty"
    assert result.frequencies[int(np.argmax(result.spectrum[0]))] == pytest.approx(1000, abs=30)


def test_the_envelope_spans_the_whole_track(clips):
    result = analysis.analyse(probe_track(clips["dual_mono"]))

    assert result.envelope.shape[1] == 2, "one column per channel"
    assert result.envelope.shape[2] == 2, "a minimum and a maximum per window"
    assert result.envelope.shape[0] > 100
    # Minimums are negative and maximums positive, or the two are swapped.
    assert result.envelope[:, :, 0].min() < 0 < result.envelope[:, :, 1].max()


def test_a_file_with_no_audio_is_an_error(tmp_path, ffmpeg):
    silent = tmp_path / "video.mkv"
    subprocess.run(  # noqa: S603
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=24:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", str(silent),
        ],  # fmt: skip
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip

    from kiyas.media.probe import ProbeError

    with pytest.raises(ProbeError, match="no audio stream"):
        probe_track(silent)


# --------------------------------------------------------------------------
# Offset
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def delayed(tmp_path_factory, ffmpeg) -> dict[str, Path]:
    """Noise, and the same noise moved by a known amount."""
    directory = tmp_path_factory.mktemp("audio-sync")
    reference = directory / "ref.flac"
    subprocess.run(  # noqa: S603
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anoisesrc=duration=20:sample_rate=48000:seed=7",
            "-af", "volume=0.3", "-c:a", "flac", str(reference),
        ],  # fmt: skip
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip

    late = directory / "late.flac"
    subprocess.run(  # noqa: S603
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(reference),
         "-af", "adelay=250", "-c:a", "flac", str(late)],  # fmt: skip
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip

    early = directory / "early.flac"
    subprocess.run(  # noqa: S603
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.25",
         "-i", str(reference), "-c:a", "flac", str(early)],  # fmt: skip
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip

    return {"reference": reference, "late": late, "early": early}


def test_a_delayed_track_measures_as_later(delayed):
    """The sign convention, checked rather than assumed.

    Getting it backwards produces a correction in the wrong direction, which is
    twice as wrong as doing nothing.
    """
    from kiyas.audio import sync

    offset = sync.measure(probe_track(delayed["reference"]), probe_track(delayed["late"]))

    assert offset.milliseconds == pytest.approx(250, abs=1)
    assert not offset.is_weak


def test_an_advanced_track_measures_as_earlier(delayed):
    from kiyas.audio import sync

    offset = sync.measure(probe_track(delayed["reference"]), probe_track(delayed["early"]))

    assert offset.milliseconds == pytest.approx(-250, abs=1)


def test_a_track_against_itself_measures_zero(delayed):
    from kiyas.audio import sync

    offset = sync.measure(probe_track(delayed["reference"]), probe_track(delayed["reference"]))

    assert offset.milliseconds == pytest.approx(0, abs=0.5)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def test_the_specification_table_shows_both_the_claim_and_the_measurement(clips):
    padded = analysis.analyse(probe_track(clips["padded"]))

    text = table.markdown([padded])

    assert "declared bit depth" in text
    assert "measured bit depth" in text
    assert text.count("| **declared bit depth**") == 1, "one row per label, not two"


def test_the_table_carries_the_offset_and_its_direction(clips):
    first = analysis.analyse(probe_track(clips["sixteen"]))
    second = analysis.analyse(probe_track(clips["lossy"]))

    text = table.markdown([first, second], offsets=[Offset(-12.5, 40.0, "GCC-PHAT")])

    assert "12.5 ms earlier" in text


def test_the_bbcode_table_has_a_column_per_track(clips):
    first = analysis.analyse(probe_track(clips["sixteen"]))
    second = analysis.analyse(probe_track(clips["lossy"]))

    text = table.bbcode([first, second])

    assert text.startswith("[table]")
    assert text.rstrip().endswith("float output that uses every bit whatever the source was")
    assert "[td][b]codec[/b][/td][td]flac[/td][td]ac3[/td][/tr]" in text


def test_an_empty_comparison_produces_nothing_rather_than_a_broken_table():
    assert table.markdown([]) == ""
    assert table.bbcode([]) == ""


def test_a_full_run_produces_a_publishable_comparison(clips, tmp_path):
    from kiyas.audio import run as audio_run
    from kiyas.publish import load_manifest

    result = audio_run.run(
        [clips["sixteen"], clips["lossy"]], output=tmp_path / "out", title="Two tracks"
    )

    assert result.image_count == len(audio_run.ANALYSES) * 2
    assert result.specifications.is_file()

    # The point of matching the picture side's layout: publishing reads it back.
    comparison = load_manifest(tmp_path / "out")
    assert comparison.title == "Two tracks"
    assert [row.label for row in comparison.rows] == [label for label, _ in audio_run.ANALYSES]
    assert len(comparison.sources) == 2


def test_two_files_with_the_same_name_get_different_columns(clips, tmp_path):
    """Otherwise they share a directory and publish as one track twice."""
    from kiyas.audio import run as audio_run

    first = tmp_path / "a" / "same.flac"
    second = tmp_path / "b" / "same.flac"
    for destination in (first, second):
        destination.parent.mkdir(parents=True)
        destination.write_bytes(clips["sixteen"].read_bytes())

    result = audio_run.run([first, second], output=tmp_path / "out")

    names = [track.name for track in result.tracks]
    assert len(set(names)) == 2
    assert len({track.directory for track in result.tracks}) == 2


def test_running_with_no_files_is_refused(tmp_path):
    from kiyas.audio import run as audio_run

    with pytest.raises(AnalysisError, match="at least one"):
        audio_run.run([], output=tmp_path / "out")


def test_a_missing_file_is_named(tmp_path):
    from kiyas.audio import run as audio_run

    with pytest.raises(AnalysisError, match="do not exist"):
        audio_run.run([tmp_path / "nope.flac"], output=tmp_path / "out")
