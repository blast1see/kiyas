from __future__ import annotations

from fractions import Fraction

import pytest

from kiyas.config import FrameMethod, FrameSelection
from kiyas.frames import selector
from kiyas.frames.selector import SelectionError

FPS = Fraction(24000, 1001)
TOTAL = 100_000


def sel(**overrides) -> FrameSelection:
    selection = FrameSelection()
    for key, value in overrides.items():
        setattr(selection, key, value)
    return selection


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


def test_window_skips_head_and_tail():
    """Logos, black leader and credits are identical across releases.

    They are also exactly what evenly spaced sampling lands on first.
    """
    window = selector.window_for(sel(skip_start=0.05, skip_end=0.10), 1000)

    assert window.start == 50
    assert window.end == 900


def test_window_can_cover_everything():
    window = selector.window_for(sel(skip_start=0.0, skip_end=0.0), 1000)

    assert (window.start, window.end) == (0, 1000)


def test_window_falls_back_on_a_very_short_clip():
    """Percentages collapse on a 5-frame clip; sampling it all beats refusing."""
    window = selector.window_for(sel(skip_start=0.4, skip_end=0.4), 2)

    assert window.length > 0


def test_window_rejects_an_empty_clip():
    with pytest.raises(SelectionError, match="no frames"):
        selector.window_for(sel(), 0)


# --------------------------------------------------------------------------
# select()
# --------------------------------------------------------------------------


def test_count_produces_that_many_frames():
    frames = selector.select(sel(method=FrameMethod.COUNT, count=12), TOTAL, FPS)

    assert len(frames) == 12


def test_frames_are_sorted_and_unique():
    frames = selector.select(sel(count=20), TOTAL, FPS)

    assert frames == sorted(set(frames))


def test_frames_stay_inside_the_window():
    selection = sel(count=30, skip_start=0.05, skip_end=0.10)
    window = selector.window_for(selection, TOTAL)

    frames = selector.select(selection, TOTAL, FPS)

    assert all(window.start <= f < window.end for f in frames)


def test_first_frame_is_inset_from_the_window_edge():
    """Landing exactly on the boundary usually means a fade out of the logo."""
    selection = sel(count=10, skip_start=0.05, skip_end=0.10)
    window = selector.window_for(selection, TOTAL)

    frames = selector.select(selection, TOTAL, FPS)

    assert frames[0] > window.start


def test_single_frame_lands_in_the_middle():
    frames = selector.select(sel(count=1, skip_start=0.0, skip_end=0.0), 1000, FPS)

    assert frames == [500]


def test_interval_respects_the_frame_rate():
    """5 minutes at 24000/1001 is 7192 frames, not 7200."""
    selection = sel(
        method=FrameMethod.INTERVAL, interval_seconds=300.0, skip_start=0.0, skip_end=0.0
    )

    frames = selector.select(selection, TOTAL, FPS)

    assert frames[1] - frames[0] == round(float(FPS) * 300)


def test_interval_on_a_clip_shorter_than_one_interval():
    selection = sel(method=FrameMethod.INTERVAL, interval_seconds=300.0)

    frames = selector.select(selection, 100, FPS)

    assert len(frames) == 1


def test_manual_frames_are_used_verbatim():
    selection = sel(method=FrameMethod.MANUAL, manual=(100, 5000, 90000))

    assert selector.select(selection, TOTAL, FPS) == [100, 5000, 90000]


def test_manual_frames_ignore_the_skip_window():
    """An explicitly chosen frame is a decision, not a suggestion."""
    selection = sel(method=FrameMethod.MANUAL, manual=(5,), skip_start=0.4, skip_end=0.4)

    assert selector.select(selection, 1000, FPS) == [5]


def test_manual_frame_past_the_end_is_reported_with_a_hint():
    """Trim shifts these numbers; that is nearly always the cause."""
    selection = sel(method=FrameMethod.MANUAL, manual=(10, 999_999))

    with pytest.raises(SelectionError, match="Trim values shift these"):
        selector.select(selection, TOTAL, FPS)


def test_all_manual_frames_out_of_range():
    selection = sel(method=FrameMethod.MANUAL, manual=(500_000,))

    with pytest.raises(SelectionError, match="within the clip"):
        selector.select(selection, TOTAL, FPS)


def test_empty_clip_is_rejected():
    with pytest.raises(SelectionError):
        selector.select(sel(), 0, FPS)


def test_more_frames_requested_than_exist():
    frames = selector.select(sel(count=50, skip_start=0.0, skip_end=0.0), 10, FPS)

    assert frames == sorted(set(frames))
    assert all(0 <= f < 10 for f in frames)


# --------------------------------------------------------------------------
# Seeded sampling
# --------------------------------------------------------------------------


def test_seed_makes_selection_reproducible():
    a = selector.select(sel(count=15, seed=42), TOTAL, FPS)
    b = selector.select(sel(count=15, seed=42), TOTAL, FPS)

    assert a == b


def test_different_seeds_give_different_frames():
    a = selector.select(sel(count=15, seed=1), TOTAL, FPS)
    b = selector.select(sel(count=15, seed=2), TOTAL, FPS)

    assert a != b


def test_seeded_selection_differs_from_even_spacing():
    """Even spacing can land on the same structural beat every time."""
    even = selector.select(sel(count=15), TOTAL, FPS)
    seeded = selector.select(sel(count=15, seed=7), TOTAL, FPS)

    assert even != seeded


def test_seeded_selection_stays_in_the_window():
    selection = sel(count=15, seed=3, skip_start=0.05, skip_end=0.10)
    window = selector.window_for(selection, TOTAL)

    frames = selector.select(selection, TOTAL, FPS)

    assert all(window.start <= f < window.end for f in frames)


# --------------------------------------------------------------------------
# refine()
# --------------------------------------------------------------------------


def test_refine_keeps_acceptable_frames_untouched():
    frames, rejected, _ = selector.refine([10, 20, 30], 1000, lambda _f: True)

    assert frames == [10, 20, 30]
    assert rejected == []


def test_refine_nudges_forward_to_the_next_acceptable_frame():
    """B-frames are what the rule is really after.

    I-frames sit at different places in every encode and get a
    disproportionate share of the bitrate, so comparing them flatters a bad
    encode.
    """
    acceptable = {13, 24, 30}

    frames, rejected, _ = selector.refine([10, 20, 30], 1000, lambda f: f in acceptable)

    assert frames == [13, 24, 30]
    assert rejected == []


def test_refine_never_searches_backwards():
    """Two samples converging from opposite directions would silently duplicate.

    Frame 50 is nearer than 130, so a bidirectional search would pick it.
    Both candidates sit inside MAX_NUDGE of the requested frame, so the limit
    is not what decides this.
    """
    frames, _, _ = selector.refine([100], 1000, lambda f: f in {50, 130})

    assert frames == [130]


def test_refine_does_not_reuse_a_frame():
    """Without this, two nearby samples both nudge onto the same frame and the
    comparison quietly contains the same picture twice."""
    acceptable = {500}

    frames, rejected, _ = selector.refine([498, 499], 1000, lambda f: f in acceptable)

    assert frames == [500]
    assert rejected == [499]


def test_refine_reports_positions_it_could_not_resolve():
    frames, rejected, _ = selector.refine([10, 500], 1000, lambda f: f == 10)

    assert frames == [10]
    assert rejected == [500]


def test_refine_respects_an_explicit_nudge_limit():
    frames, rejected, _ = selector.refine(
        [100], 10_000, lambda f: f == 100 + selector.MIN_NUDGE + 1, max_nudge=selector.MIN_NUDGE
    )

    assert frames == []
    assert rejected == [100]


def test_the_nudge_budget_scales_with_the_spacing():
    """A dark scene runs for minutes, and 48 frames is two seconds.

    With a fixed budget, asking for three frames of a night-lit film returned
    one: the other two positions landed in the dark and there was nowhere to
    go. The budget is a share of the gap to the next requested position, so it
    grows with the film and with how few frames were asked for.
    """
    frames, rejected, _ = selector.refine([1000, 100_000], 200_000, lambda f: f >= 2000)

    assert rejected == []
    # Sampled rather than walked past the first two seconds, so it lands on the
    # first candidate at or after the acceptable point, not exactly on it.
    assert 2000 <= frames[0] < 2000 + selector.COARSE_STEP


def test_a_nudged_frame_cannot_reach_the_next_requested_one():
    """Otherwise two samples become one moment and the comparison has a hole."""
    frames, rejected, _ = selector.refine([1000, 2000], 100_000, lambda f: f >= 1999)

    assert rejected == [1000], "it should give up rather than land on its neighbour"
    assert frames == [2000], "the second position resolves to itself"


def test_a_frame_that_travels_a_long_way_is_reported():
    """Moving a few frames is the rule working; moving half a minute is a
    different moment, and the person should be told."""
    target = 1000 + selector.NOTABLE_MOVE
    _, _, moved = selector.refine([1000], 200_000, lambda f: f >= target)

    assert len(moved) == 1
    was, now = moved[0]
    assert was == 1000
    assert target <= now < target + selector.COARSE_STEP


def test_a_short_move_is_not_worth_mentioning():
    _, _, moved = selector.refine([1000], 200_000, lambda f: f >= 1003)

    assert moved == []


def test_refine_does_not_run_past_the_end_of_the_clip():
    frames, rejected, _ = selector.refine([995], 1000, lambda _f: False)

    assert frames == []
    assert rejected == [995]


def test_refine_output_is_sorted():
    frames, _, _ = selector.refine([50, 10], 1000, lambda _f: True)

    assert frames == sorted(frames)


# --------------------------------------------------------------------------
# describe()
# --------------------------------------------------------------------------


def test_describe_renders_timestamps():
    assert selector.describe([0], Fraction(24)) == ["0 (0:00:00)"]
    assert selector.describe([24 * 65], Fraction(24)) == ["1560 (0:01:05)"]
    assert selector.describe([24 * 3725], Fraction(24)) == ["89400 (1:02:05)"]


# ---------------------------------------------------------------- extremes --


def _brightness(pattern: dict[int, float]):
    """A luma callable: named frames get their value, everything else 0.5."""
    return lambda frame: pattern.get(frame, 0.5)


def test_asking_for_nothing_looks_at_nothing():
    """The pool costs a decode per candidate, so it must not be built unasked."""
    looked: list[int] = []

    picked = selector.extremes(
        selector.Window(0, TOTAL), 0, 0, lambda frame: looked.append(frame) or 0.5
    )

    assert picked == []
    assert looked == []


def test_the_darkest_sampled_frame_is_picked():
    window = selector.Window(0, 10_000)
    pool = selector._evenly_spaced(window, selector.POOL_PER_PICK)

    picked = selector.extremes(window, 1, 0, _brightness({pool[3]: 0.02}))

    assert picked == [pool[3]]


def test_the_brightest_sampled_frame_is_picked():
    window = selector.Window(0, 10_000)
    pool = selector._evenly_spaced(window, selector.POOL_PER_PICK)

    picked = selector.extremes(window, 0, 1, _brightness({pool[7]: 0.99}))

    assert picked == [pool[7]]


def test_both_ends_come_back_together():
    window = selector.Window(0, 10_000)
    pool = selector._evenly_spaced(window, 2 * selector.POOL_PER_PICK)

    picked = selector.extremes(window, 1, 1, _brightness({pool[2]: 0.01, pool[9]: 0.99}))

    assert picked == sorted([pool[2], pool[9]])


def test_a_frame_already_chosen_is_not_chosen_twice():
    """The extremes are added to the evenly spaced set, not merged into it."""
    window = selector.Window(0, 10_000)
    pool = selector._evenly_spaced(window, selector.POOL_PER_PICK)

    picked = selector.extremes(
        window, 1, 0, _brightness({pool[0]: 0.01, pool[1]: 0.02}), avoid={pool[0]}
    )

    assert picked == [pool[1]]


def test_the_black_frame_floor_still_applies():
    """A fade to black is the darkest frame of most films and compares nothing.

    `skip_dark` is not in tension with asking for dark frames; it is what makes
    the request mean something, so the same predicate gates both.
    """
    window = selector.Window(0, 10_000)
    pool = selector._evenly_spaced(window, selector.POOL_PER_PICK)
    luma = _brightness({pool[0]: 0.001, pool[4]: 0.05})

    picked = selector.extremes(window, 1, 0, luma, acceptable=lambda frame: luma(frame) >= 0.01)

    assert picked == [pool[4]]


def test_nothing_acceptable_returns_nothing_rather_than_a_bad_frame():
    picked = selector.extremes(
        selector.Window(0, 10_000), 2, 2, _brightness({}), acceptable=lambda _frame: False
    )

    assert picked == []
