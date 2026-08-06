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
from collections.abc import Callable
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


#: How far forward :func:`refine` will look for an acceptable frame.
#:
#: Two seconds at 24fps. A B-frame is never more than a GOP away (a couple of
#: dozen frames at most), and a dark stretch longer than this is a deliberate
#: scene rather than a fade -- at which point moving further has drifted to a
#: different shot and stopped being the frame the user asked for.
MAX_NUDGE = 48


def refine(
    frames: list[int],
    total: int,
    acceptable: Callable[[int], bool],
    *,
    max_nudge: int = MAX_NUDGE,
) -> tuple[list[int], list[int]]:
    """Nudge each frame forward until ``acceptable`` says yes.

    Returns ``(frames, rejected)`` where ``rejected`` lists the original
    positions no acceptable frame was found near. Callers report those rather
    than silently capturing a bad frame or silently dropping a good position.

    Frames are searched forward only. Searching backwards as well would let two
    adjacent samples converge on the same frame from opposite directions, and
    the resulting duplicate is invisible until it shows up twice in the
    published comparison.
    """
    resolved: list[int] = []
    rejected: list[int] = []
    taken: set[int] = set()

    for frame in frames:
        found = None
        for candidate in range(frame, min(frame + max_nudge + 1, total)):
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

    return sorted(resolved), rejected


def describe(frames: list[int], fps: Fraction) -> list[str]:
    """Human-readable timestamps, for logs and for the frame list on disk."""
    out = []
    for frame in frames:
        seconds = frame / float(fps)
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        out.append(f"{frame} ({hours:d}:{minutes:02d}:{secs:02d})")
    return out
