"""Measuring how far apart two sources are.

Nothing here decodes anything, for the same reason nothing in
``test_selector.py`` does: the module takes callables that hand back luma
thumbnails, so the search and the score can be pinned down exactly, on content
whose answer is known, without waiting for a feature film.

The two tests that matter most are the monotonic-curve one and the sign one.
The first is the entire reason the score is a rank correlation rather than a
difference -- a grade, a tone curve and a bitrate all move every value while
reordering almost none -- and without it somebody will simplify it to a
difference and it will keep passing on identical clips. The second is because
a sign error here does not fail: it corrects in the wrong direction and
doubles the error it was measuring.
"""

from __future__ import annotations

import random
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from kiyas.frames import align

FPS = Fraction(24000, 1001)
PIXELS = 64 * 36


def _film(length: int, *, seed: int = 11) -> list[bytes]:
    """Frames that behave like a film: shots, with motion inside them.

    Independent random frames would make this too easy and a single still
    would make it impossible; a film is neither. Shots change every fifty
    frames and drift a little in between, which is what gives a correlation
    search something to lock onto and something to be wrong about.
    """
    rng = random.Random(seed)
    frames: list[bytes] = []
    current = [rng.randrange(256) for _ in range(PIXELS)]
    for index in range(length):
        if index % 50 == 0:
            current = [rng.randrange(256) for _ in range(PIXELS)]
        current = [max(0, min(255, value + rng.randint(-2, 2))) for value in current]
        frames.append(bytes(current))
    return frames


def _reader(frames: list[bytes], shift: int = 0, transform=None):
    """A ``luma_thumbnails``-shaped callable over ``frames``."""

    def read(start: int, count: int, step: int = 1) -> list[bytes]:
        out = []
        for index in range(start, start + count, max(1, step)):
            source = index - shift
            if 0 <= source < len(frames):
                frame = frames[source]
                out.append(transform(frame) if transform else frame)
        return out

    return read


def _measure(frames, shift, transform=None, window=60):
    return align.measure(
        "B",
        _reader(frames),
        _reader(frames, shift, transform),
        positions=[200, 400, 600],
        window=window,
        run=3,
    )


# --------------------------------------------------------------------------
# The score
# --------------------------------------------------------------------------


def test_ranks_average_ties_rather_than_inventing_an_order():
    """A dark shot puts thousands of pixels in one bin.

    Ranking those arbitrarily would invent ordering the picture does not have,
    and the correlation would then be measuring the invention.
    """
    result = align.ranks(bytes([5, 5, 5, 200]))

    assert result[:3] == [1.0, 1.0, 1.0]
    assert result[3] == 3.0


def test_a_monotonic_curve_does_not_change_the_ranks():
    """The whole reason the score is a rank correlation.

    A grade, a tone curve and a lower bitrate all move every value; none of
    them reorders one. A difference-based score would call this a mismatch.
    """
    thumbnail = bytes(range(256)) * 9
    gamma = bytes(min(255, int((value / 255) ** 0.45 * 255)) for value in thumbnail)

    # Not exactly 1.0, and the reason is worth knowing: this curve maps 256
    # distinct values onto 184, so it creates ties that were not in the
    # original. Monotonic is not the same as injective, and an 8-bit output
    # cannot be. A difference-based score on the same pair is nowhere near.
    assert align.similarity(align.ranks(thumbnail), align.ranks(gamma)) > 0.9999


def test_a_flat_frame_correlates_with_nothing():
    """Black leader has no variance, and "perfect match" there is the
    strongest possible wrong answer."""
    flat = bytes(PIXELS)

    assert align.similarity(align.ranks(flat), align.ranks(_film(1)[0])) == 0.0


# --------------------------------------------------------------------------
# The search
# --------------------------------------------------------------------------


def test_a_known_shift_is_found_exactly():
    frames = _film(900)

    assert _measure(frames, 7).frames == 7


def test_the_sign_says_which_source_plays_later():
    """Stated, not implied. Backwards doubles the error it was measuring.

    Positive means this source plays later than the reference, which is the
    number to add to its trim.
    """
    frames = _film(900)

    assert _measure(frames, 11).frames == 11
    assert _measure(frames, -11).frames == -11


def test_sources_already_in_step_measure_zero():
    frames = _film(900)

    assert _measure(frames, 0).frames == 0


def test_a_gamma_shifted_copy_is_still_matched():
    """Two releases of a title differ by a grade before they differ by an offset."""
    frames = _film(900)

    def gamma(frame: bytes) -> bytes:
        return bytes(min(255, int((value / 255) ** 0.45 * 255)) for value in frame)

    result = _measure(frames, 7, gamma)

    assert result.frames == 7
    assert not result.is_weak


def test_unrelated_material_is_reported_as_a_guess():
    """A number with no confidence attached is worse than no number.

    Measured on the synthetic material here: a genuine match scores about 34
    peak-to-median and unrelated content about 4, which is where the threshold
    sits.
    """
    result = align.measure(
        "B",
        _reader(_film(900, seed=1)),
        _reader(_film(900, seed=2)),
        positions=[200, 400, 600],
        window=60,
        run=3,
    )

    assert result.is_weak


def test_a_real_match_is_not_reported_as_a_guess():
    assert not _measure(_film(900), 7).is_weak


def test_a_wide_window_is_searched_coarsely_and_then_exactly():
    """The path every real comparison takes, and the only one that is fast.

    One percent of a feature is thousands of frames either side, and searching
    that at full rate is tens of thousands of 4K frames per source. The coarse
    pass strides over it and the fine pass fixes the remainder, so the answer
    still has to be exact -- a trim right to within a second is not a trim.
    """
    frames = _film(6000)
    window = 1400
    assert (window * 2 + 1) // align.COARSE_STEP >= align.MIN_COARSE_FIELD, "not the coarse path"

    result = align.measure(
        "B",
        _reader(frames),
        _reader(frames, 37),
        positions=[2000, 3000, 4000],
        window=window,
        run=3,
    )

    assert result.frames == 37
    assert not result.is_weak


def test_a_narrow_window_skips_the_coarse_pass():
    """Striding a small window leaves a field too short to take a median of.

    Measured: on a 60-frame window it dropped a correct answer's confidence
    from 34 to 1.9, which reports a right answer as a guess.
    """
    frames = _film(900)
    span = 60 * 2 + 3
    assert span // align.COARSE_STEP < align.MIN_COARSE_FIELD

    assert not _measure(frames, 7).is_weak


# --------------------------------------------------------------------------
# Window sizing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("total", [96, 1000, 209389])
def test_the_window_is_a_share_of_the_material_with_a_floor(total):
    """One percent of a four-second clip is one frame, and a search across one
    candidate can only ever return zero."""
    window = align.window_for(total)

    assert window >= align.MIN_WINDOW
    assert window >= int(total * align.WINDOW_SHARE)


def test_a_feature_gets_a_window_wider_than_any_hand_set_trim():
    """About ninety seconds either way on a two-hour film."""
    assert align.window_for(209389) > 2000


# --------------------------------------------------------------------------
# What gets printed
# --------------------------------------------------------------------------


def test_a_suggested_trim_is_never_negative():
    """``trim = -5`` is valid Python: ``clip[-5:]`` takes the last five frames.

    Handing someone a negative number to paste would be handing them a
    comparison of the closing credits. A source that plays earlier moves the
    whole set instead.
    """
    trims = align.suggested_trims([0, -5], [0, 0])

    assert min(trims) == 0
    assert trims == [5, 0]


def test_an_existing_trim_is_carried_into_the_suggestion():
    """The measurement is a residual, because the sources were prepared with
    the trims already applied, so it adds to what is there."""
    assert align.suggested_trims([0, 3], [0, 24]) == [0, 27]


def test_the_reference_is_what_moves_when_another_source_plays_earlier():
    """There is no negative trim, so the reference takes the shift instead."""
    assert align.suggested_trims([0, -5], [0, 0]) == [5, 0]


def test_a_weak_result_says_so_in_its_summary():
    """Three of nine positions finding the same offset is not an answer."""
    weak = align.FrameOffset("B", 5, 3, 9, 60)

    assert "weak match" in weak.summary(FPS)
    assert "3 of 9" in weak.summary(FPS)


def test_a_result_most_positions_agree_on_is_not_called_a_guess():
    assert not align.FrameOffset("B", 5, 8, 9, 60).is_weak


def test_a_measurement_that_sampled_nothing_is_weak():
    """Zero of zero is not unanimity."""
    assert align.FrameOffset("B", 0, 0, 0, 60).is_weak


def test_the_summary_names_the_direction_and_the_time():
    later = align.FrameOffset("B", 24, 9, 9, 2000)

    text = later.summary(FPS)

    assert "later" in text
    assert "1.00s" in text


# --------------------------------------------------------------------------
# Through a real engine
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shifted_clips(tmp_path_factory):
    """Two encodes of one clip, the second starting a known 7 frames later.

    Cut from the same source with ``select``, so the offset is exact rather
    than a seek's opinion, and encoded at different qualities so the two are
    not byte-identical -- an alignment that only works on identical pictures
    is not an alignment.
    """
    from kiyas.media import binaries

    ffmpeg = binaries.find_binary("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")

    directory = tmp_path_factory.mktemp("align-media")
    master = directory / "master.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=20",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-g",
            "12",
            "-bf",
            "3",
            str(master),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )

    def cut(name: str, first: int, crf: str) -> Path:
        target = directory / name
        subprocess.run(
            [
                str(ffmpeg),
                "-y",
                "-v",
                "error",
                "-i",
                str(master),
                "-vf",
                f"select='gte(n,{first})',setpts=N/FRAME_RATE/TB",
                "-c:v",
                "libx264",
                "-crf",
                crf,
                "-g",
                "12",
                "-bf",
                "3",
                str(target),
            ],  # fmt: skip
            check=True,
            capture_output=True,
        )
        return target

    return {"a": cut("a.mkv", 0, "18"), "b": cut("b.mkv", 7, "30"), "dir": directory}


@pytest.mark.integration
def test_a_known_offset_is_measured_through_the_ffmpeg_engine(shifted_clips):
    """The engine seam, not just the arithmetic.

    b starts 7 frames into a, so a's frame N is b's frame N-7 and b has to
    report -7: it plays *earlier* relative to the same content.
    """
    from kiyas.config import Source
    from kiyas.engines.ffmpeg import FfmpegEngine

    engine = FfmpegEngine()
    first = engine.prepare(Source(path=shifted_clips["a"], name="A"), overlay=False)
    second = engine.prepare(Source(path=shifted_clips["b"], name="B"), overlay=False)

    result = align.measure(
        "B",
        first.luma_thumbnails,
        second.luma_thumbnails,
        positions=[120, 240, 360],
        window=align.MIN_WINDOW,
        run=3,
    )

    assert result.frames == -7


@pytest.mark.integration
def test_luma_thumbnails_and_mean_luma_agree(shifted_clips):
    """One reduction, not two.

    They used to be separate implementations of the same thing, and the last
    time the engines measured brightness differently they returned opposite
    verdicts on two frames out of six.
    """
    from kiyas.config import Source
    from kiyas.engines.ffmpeg import FfmpegEngine

    prepared = FfmpegEngine().prepare(Source(path=shifted_clips["a"], name="A"), overlay=False)

    thumbnail = prepared.luma_thumbnails(100, 1)[0]

    assert sum(thumbnail) / (len(thumbnail) * 255.0) == pytest.approx(
        prepared.mean_luma(100), abs=0.01
    )


@pytest.mark.integration
def test_a_run_of_thumbnails_is_actually_consecutive(shifted_clips):
    """Asked for a run, the engine has to hand back that run.

    ffmpeg's default pacing rewrites the output to a constant frame rate,
    duplicating and dropping frames to fit, and the result still looks like a
    contiguous run. Measured before this was fixed: one duplicate near the
    start put 45 of 48 thumbnails one frame late, which is a systematic
    one-frame bias in every offset the module would go on to measure.

    Fetching them one at a time cannot drift, so it is the reference.
    """
    from kiyas.config import Source
    from kiyas.engines.ffmpeg import FfmpegEngine

    prepared = FfmpegEngine().prepare(Source(path=shifted_clips["a"], name="A"), overlay=False)

    run = prepared.luma_thumbnails(100, 8)
    one_at_a_time = [prepared.luma_thumbnails(100 + index, 1)[0] for index in range(8)]

    assert run == one_at_a_time


@pytest.mark.integration
def test_a_stride_returns_every_nth_thumbnail(shifted_clips):
    """The coarse pass depends on this, and it is the only place it is used."""
    from kiyas.config import Source
    from kiyas.engines.ffmpeg import FfmpegEngine

    prepared = FfmpegEngine().prepare(Source(path=shifted_clips["a"], name="A"), overlay=False)

    strided = prepared.luma_thumbnails(100, 48, 24)
    every = prepared.luma_thumbnails(100, 48)

    assert len(strided) == 2
    assert strided[0] == every[0]
    assert strided[1] == every[24]
