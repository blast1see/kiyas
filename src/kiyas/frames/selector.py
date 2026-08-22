"""Choosing which frames a comparison is built from.

Split in two on purpose:

:func:`select` is arithmetic -- window, spacing, bounds. It knows nothing about
video and needs no decoder, so its rules are cheap to test exhaustively.

:func:`refine` applies the rules that need to *look* at the frames (is this a
B-frame? is it too dark to show anything?). It takes a predicate rather than a
clip, so this module never imports VapourSynth and the engines never reimplement
the search.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fractions import Fraction

from ..config import FrameMethod, FrameSelection


class SelectionError(ValueError):
    """Raised when no usable set of frames can be produced."""


@dataclass(frozen=True, slots=True)
class Window:
    """The usable span of a clip, in frames: ``[start, end)``."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)


def window_for(selection: FrameSelection, total: int) -> Window:
    """The part of the clip worth sampling.

    The head and tail are skipped by default because studio logos, black
    leader and end credits are identical across every release of a title.
    Screenshots of them compare nothing, and they are exactly what evenly
    spaced sampling lands on first.
    """
    if total <= 0:
        raise SelectionError("the clip reports no frames")

    start = int(total * selection.skip_start)
    end = total - int(total * selection.skip_end)

    if end - start < 1:
        # Only reachable on a clip so short the percentages collapse; sampling
        # the whole thing beats refusing.
        return Window(0, total)
    return Window(start, end)


def _evenly_spaced(window: Window, count: int) -> list[int]:
    """``count`` frames spread across ``window``, inset from both edges.

    The half-step inset matters: without it the first sample sits exactly on
    the window boundary, which after a 5% head skip is often the first frame
    after the studio logo -- a fade, and useless for judging an encode.
    """
    if count == 1:
        return [window.start + window.length // 2]
    step = window.length / count
    return [window.start + int((index + 0.5) * step) for index in range(count)]


def _at_interval(window: Window, fps: Fraction, seconds: float) -> list[int]:
    stride = max(1, int(round(float(fps) * seconds)))
    frames = list(range(window.start, window.end, stride))
    return frames or [window.start]


def _random(window: Window, count: int, seed: int) -> list[int]:
    """Reproducible random sampling.

    Evenly spaced frames can land systematically on the same structural beat of
    a film -- a cut every N seconds is not unusual -- and then every screenshot
    is a fade. A seed keeps a comparison reproducible while breaking that
    pattern.
    """
    rng = random.Random(seed)
    if count >= window.length:
        return list(range(window.start, window.end))
    return sorted(rng.sample(range(window.start, window.end), count))


def select(selection: FrameSelection, total: int, fps: Fraction) -> list[int]:
    """The frame numbers to capture, before any content-aware refinement."""
    if total <= 0:
        raise SelectionError("the clip reports no frames")

    if selection.method is FrameMethod.MANUAL:
        chosen = [f for f in selection.manual if 0 <= f < total]
        out_of_range = [f for f in selection.manual if f >= total]
        if not chosen:
            raise SelectionError(
                f"none of the manual frame numbers are within the clip "
                f"(it has {total} frames, requested {sorted(selection.manual)})"
            )
        if out_of_range:
            raise SelectionError(
                f"manual frames {out_of_range} are past the end of the clip, "
                f"which has {total} frames. Trim values shift these -- check "
                f"whether the numbers came from an untrimmed preview."
            )
        return chosen

    window = window_for(selection, total)

    if selection.method is FrameMethod.INTERVAL:
        frames = _at_interval(window, fps, selection.interval_seconds)
    elif selection.seed is not None:
        frames = _random(window, selection.count, selection.seed)
    else:
        frames = _evenly_spaced(window, selection.count)

    # Short clips can collapse several samples onto one frame.
    return sorted(set(frames))


#: The least :func:`refine` will look forward, whatever the spacing.
#:
#: Two seconds at 24fps, which is all the B-frame rule ever needs: a B-frame is
#: never more than a GOP away.
MIN_NUDGE = 48

#: ...but the dark-frame rule needs orders of magnitude more, and this is the
#: third time an absolute threshold in frames has been wrong here.
#:
#: A dark *scene* runs for minutes, not for two seconds. With 48 frames of
#: budget, asking for three frames of a night-lit film returned one -- the
#: other two positions landed in the dark and there was nowhere to go. The
#: budget is a share of the distance to the next requested position instead, so
#: it scales with the film and with how many frames were asked for, and a
#: nudged frame can never reach the position after it.
NUDGE_SHARE = 0.25

#: How far a frame can move before it is worth mentioning. Moving a few frames
#: is the rule working; moving thirty seconds means the answer is a different
#: moment from the one that was asked for, and the person should know.
NOTABLE_MOVE = 240

#: Past :data:`MIN_NUDGE`, candidates are sampled rather than walked.
#:
#: The two rules want different granularities and it costs to pretend
#: otherwise. A B-frame is decided frame by frame, so the first stretch is
#: walked one at a time. Brightness is a property of a *shot*: it does not
#: change between adjacent frames, and checking every one of several thousand
#: is thousands of probes for an answer that changes about once a second. With
#: the ffmpeg engine each probe is a process launch, so walking a
#: three-thousand-frame budget took longer than the rest of the run put
#: together -- measured, on a 4K WEB-DL, before this existed.
COARSE_STEP = 24


def _candidates(start: int, budget: int, limit: int) -> Iterator[int]:
    """Frames to try, nearest first: every one, then every COARSE_STEP."""
    close = min(start + MIN_NUDGE + 1, limit)
    yield from range(start, close)
    yield from range(close, min(start + budget + 1, limit), COARSE_STEP)


def refine(
    frames: list[int],
    total: int,
    acceptable: Callable[[int], bool],
    *,
    max_nudge: int | None = None,
) -> tuple[list[int], list[int], list[tuple[int, int]]]:
    """Nudge each frame forward until ``acceptable`` says yes.

    Returns ``(frames, rejected, moved)``: the positions that worked, the ones
    where nothing acceptable was found, and ``(from, to)`` for anything that
    travelled far enough to be a different moment. Callers report the last two
    rather than silently capturing a bad frame or silently dropping a good
    position.

    ``max_nudge`` defaults to a share of the gap to the next requested frame.
    Passing a number overrides that, which is what the tests do.

    Frames are searched forward only. Searching backwards as well would let two
    adjacent samples converge on the same frame from opposite directions, and
    the resulting duplicate is invisible until it shows up twice in the
    published comparison.
    """
    ordered = sorted(frames)
    resolved: list[int] = []
    rejected: list[int] = []
    moved: list[tuple[int, int]] = []
    taken: set[int] = set()

    for index, frame in enumerate(ordered):
        if max_nudge is not None:
            budget = max_nudge
        else:
            following = ordered[index + 1] if index + 1 < len(ordered) else total
            budget = max(MIN_NUDGE, int((following - frame) * NUDGE_SHARE))

        found = None
        for candidate in _candidates(frame, budget, total):
            if candidate in taken:
                continue
            if acceptable(candidate):
                found = candidate
                break
        if found is None:
            rejected.append(frame)
        else:
            resolved.append(found)
            taken.add(found)
            if found - frame >= NOTABLE_MOVE:
                moved.append((frame, found))

    return sorted(resolved), rejected, moved


#: How many positions are looked at for each extreme frame asked for.
#:
#: Picking the darkest frame of a film means looking at frames, and looking is
#: what costs -- one process launch each in the ffmpeg engine. Twelve
#: candidates per pick is enough for the darkest of them to actually be a dark
#: shot rather than the dimmest of three well-lit ones, and small enough that
#: asking for two of each costs about the same as the ordinary selection does.
POOL_PER_PICK = 12


def extremes(
    window: Window,
    dark: int,
    light: int,
    brightness: Callable[[int], float],
    *,
    acceptable: Callable[[int], bool] | None = None,
    avoid: set[int] | None = None,
) -> list[int]:
    """The darkest and brightest frames in a sample of ``window``.

    Evenly spaced sampling finds the *typical* frame, which is the right
    default and no help for the two questions people actually bring to a
    comparison. Banding and block noise live in dark scenes; highlight rolloff
    and specular detail live in bright ones. Neither shows up on a shot of
    somebody talking in a lit room, and that is what even spacing mostly
    returns.

    Takes ``brightness`` rather than a clip, for the same reason :func:`refine`
    takes a predicate: this module never decodes anything.

    ``acceptable`` is the same predicate :func:`refine` uses, so a deliberately
    dark pick still has to be a B-frame and still has to clear the
    black-frame floor. That floor is not in tension with asking for dark
    frames -- it is what makes the request mean something. The darkest frame
    of a film is a fade to black, and a fade compares nothing at all.
    """
    if dark <= 0 and light <= 0:
        return []

    picks = dark + light
    pool = _evenly_spaced(window, min(picks * POOL_PER_PICK, max(window.length, 1)))
    seen = avoid or set()
    scored = [
        (brightness(frame), frame)
        for frame in pool
        if frame not in seen and (acceptable is None or acceptable(frame))
    ]
    if not scored:
        return []

    scored.sort()
    chosen = {frame for _, frame in scored[:dark]}
    chosen.update(frame for _, frame in scored[len(scored) - light :] if light > 0)
    return sorted(chosen)


def describe(frames: list[int], fps: Fraction) -> list[str]:
    """Human-readable timestamps, for logs and for the frame list on disk."""
    out = []
    for frame in frames:
        seconds = frame / float(fps)
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        out.append(f"{frame} ({hours:d}:{minutes:02d}:{secs:02d})")
    return out
