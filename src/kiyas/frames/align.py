"""How far apart two sources are, in frames.

The audio side already measures this for tracks, and the reasoning there
applies unchanged here: comparing two things that are not aligned measures the
misalignment and nothing else. A picture comparison has ``trim`` for it, set by
hand off a previewer, and until now nothing checked the number. A wrong trim is
wrong in every frame of the comparison and every frame still looks like a
frame, so there is nothing in the output to notice.

**The sign is stated, not implied.** ``frames > 0`` means the second source
plays *later* than the reference, which is the number to add to its ``trim``.
Backwards is worse than not measuring: it corrects in the wrong direction and
doubles the error.

Like :mod:`kiyas.frames.selector`, nothing here decodes anything. It takes
callables that hand back luma thumbnails and does arithmetic on them, so the
search and the score are tested without media and neither engine reimplements
either.

**Rank correlation rather than difference.** Two releases of a title differ by
a grade, a tone curve, a bitrate and often a resolution, and every one of those
moves every value while reordering almost none -- which is exactly what a
monotonic transform is. The same reasoning already appears in this project's
notes on finding a frame in mpv captures. It also absorbs the engines' own
documented disagreement: VapourSynth reads about 0.022 brighter than ffmpeg
across the board, and a constant offset cancels exactly under a rank.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction

#: How far either side of a position the search looks, as a share of the clip.
#:
#: One percent, the same share run.py's source-length warning uses and for the
#: same reason: past that a difference is not a sync error, it is a different
#: edition, and the length warning says that better than a correlation peak
#: can. On a two-hour feature this is about ninety seconds either way, which is
#: far more than any trim anybody sets by hand.
WINDOW_SHARE = 0.01

#: ...but never fewer than this many frames. Two seconds at 24fps, the same
#: floor as selector.MIN_NUDGE. One percent of a four-second test clip is a
#: single frame, and a search across one candidate can only ever return zero.
MIN_WINDOW = 48

#: How many positions along the film are sampled.
#:
#: Odd, and combined by median, so a position that lands in a shot one release
#: cut cannot carry the answer on its own -- four of nine can be nonsense and
#: the remaining five still decide it. This is the same move the audio side
#: makes with peak against floor, applied across positions instead of lags.
SAMPLES = 9

#: Below this share of sampled positions agreeing, the answer is a guess.
#:
#: Confidence is agreement between positions, not the shape of one score
#: field. The audio module's peak-to-floor ratio was tried first, because the
#: two measurements answer the same question -- and it does not transfer.
#: Its value depends on how self-similar the material is rather than on how
#: good the match is: measured on a real 4K feature a correct answer scored
#: 3.5, and on ffmpeg's `testsrc2` a correct answer scored 1.1, against a
#: threshold of 4. Both were right and both were reported as guesses, which is
#: the failure this project has a name for.
#:
#: Positions agreeing is content-independent and says something a reader can
#: act on: nine places in the film were measured and this many of them found
#: the same offset. Two thirds because the median already survives a minority
#: disagreeing -- this is about whether to trust the median at all.
AGREEMENT = 0.6

#: Stride of the first pass over a search window.
#:
#: Without it this is not a measurement anybody waits for. One percent of a
#: two-hour feature is 2093 frames either side, so nine positions at full rate
#: is roughly 37,000 4K frames decoded per source -- tens of minutes. Sampling
#: every 24th frame first cuts that to about 1,600, and a second pass at full
#: rate over the 48 frames around the winner costs another 900.
#:
#: 24 rather than something larger because it is about a second: brightness is
#: a property of a shot and a shot lasts seconds, so a coarse pass at this
#: stride still lands in the right one. The same reasoning, and the same
#: number, as selector.COARSE_STEP.
COARSE_STEP = 24

#: How many candidates the first pass has to leave before striding is worth it.
#:
#: Below this the window is small enough to search at full rate anyway, which
#: is the case a short clip is always in, and striding it would only cost
#: precision.
MIN_COARSE_FIELD = 48

#: How far a confirming position searches either side of the first answer.
#:
#: About four seconds. Wide enough that a position which does not really agree
#: lands somewhere else and says so, narrow enough that eight of them cost a
#: fraction of one wide search. A confirmation pinned to a couple of frames
#: could only ever agree, which would make the agreement meaningless.
CONFIRM_REACH = COARSE_STEP * 4


@dataclass(frozen=True, slots=True)
class FrameOffset:
    """How far the second source is from the reference, in frames."""

    name: str
    frames: int
    agreed: int
    sampled: int
    window: int

    @property
    def is_weak(self) -> bool:
        return self.sampled <= 0 or (self.agreed / self.sampled) < AGREEMENT

    def summary(self, fps: Fraction) -> str:
        if self.frames == 0:
            text = "aligned"
        else:
            direction = "later" if self.frames > 0 else "earlier"
            seconds = abs(self.frames) / float(fps) if fps else 0.0
            text = f"{abs(self.frames)} frames {direction} ({seconds:.2f}s)"
        text += f", {self.agreed} of {self.sampled} sampled positions agree"
        if self.is_weak:
            text += " -- weak match, treat this as a guess"
        return text


def window_for(total: int) -> int:
    """How far either side of a position to search, for a clip this long."""
    return max(MIN_WINDOW, int(total * WINDOW_SHARE))


def ranks(thumbnail: bytes) -> list[float]:
    """Mid-ranks of ``thumbnail``'s bytes, ties averaged.

    A counting sort over the 256 possible values rather than an actual sort:
    the input is 8-bit by construction, so this is O(n) with n = 2304 pixels
    and costs about a third of a millisecond in CPython. That number is here
    because the instinct on reading "rank correlation" is to reach for numpy,
    and numpy lives in the audio extra -- ``frames`` has to import on a base
    install.
    """
    counts = [0] * 256
    for value in thumbnail:
        counts[value] += 1

    # Mid-rank for a run of equal values is the average position that run
    # occupies. Ties are the common case here: a dark shot puts thousands of
    # pixels in the same bin, and ranking them arbitrarily would invent
    # ordering the picture does not have.
    midrank = [0.0] * 256
    seen = 0
    for value, count in enumerate(counts):
        if count:
            midrank[value] = seen + (count - 1) / 2.0
            seen += count
    return [midrank[value] for value in thumbnail]


def similarity(first: Sequence[float], second: Sequence[float]) -> float:
    """Pearson correlation of two rank vectors, in [-1, 1].

    Zero-normalised on both sides, which is what makes it survive a different
    grade, a different bitrate and a different tone curve. Returns 0.0 when
    either side has no variance at all -- a frame of flat black correlates
    with everything, and saying "perfect match" there would be the strongest
    possible wrong answer.
    """
    count = len(first)
    if count == 0 or count != len(second):
        return 0.0

    mean_a = sum(first) / count
    mean_b = sum(second) / count
    covariance = spread_a = spread_b = 0.0
    for a, b in zip(first, second):
        da = a - mean_a
        db = b - mean_b
        covariance += da * db
        spread_a += da * da
        spread_b += db * db

    if spread_a <= 0.0 or spread_b <= 0.0:
        return 0.0
    return covariance / ((spread_a**0.5) * (spread_b**0.5))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def score_field(
    reference: Sequence[bytes],
    candidates: Sequence[bytes],
) -> list[float]:
    """Similarity of each reference thumbnail's counterpart at every offset.

    ``candidates`` is a contiguous run; the returned list has one score per
    possible alignment of ``reference`` inside it, in order.
    """
    if not reference or len(candidates) < len(reference):
        return []

    reference_ranks = [ranks(thumbnail) for thumbnail in reference]
    candidate_ranks = [ranks(thumbnail) for thumbnail in candidates]

    scores = []
    for start in range(len(candidates) - len(reference) + 1):
        window = candidate_ranks[start : start + len(reference)]
        scores.append(_median([similarity(a, b) for a, b in zip(reference_ranks, window)]))
    return scores


def measure(
    name: str,
    reference: Callable[..., Sequence[bytes]],
    other: Callable[..., Sequence[bytes]],
    *,
    positions: Sequence[int],
    window: int,
    run: int = 1,
) -> FrameOffset:
    """Measure how far ``other`` is from ``reference``.

    Both callables take ``(start, count, step)`` and return that many luma
    thumbnails, which is the shape a decoder is fast at: one call per position
    rather than one per candidate offset. Asking frame by frame costs a process
    launch each in the ffmpeg engine, and a search worth doing is thousands of
    frames wide.
    """
    if not positions:
        return FrameOffset(name, 0, 0, 0, window)

    def search(position: int, centre: int, reach: int) -> int | None:
        """The best offset near ``centre``, measured at ``position``."""
        anchor = reference(position, run)
        if not anchor:
            return None

        start = position + centre - reach
        span = reach * 2 + run
        # Striding only pays on a window wide enough to still leave a field
        # worth taking a median of. A narrow one is searched at full rate,
        # which it can afford precisely because it is narrow.
        step = COARSE_STEP if span // COARSE_STEP >= MIN_COARSE_FIELD else 1
        scores = score_field(anchor, other(start, span, step))
        if not scores:
            return None

        found = start + max(range(len(scores)), key=lambda index: scores[index]) * step

        if step > 1:
            # The coarse pass can only be right to within its own stride, and
            # a trim right to within a second is not a trim.
            fine_start = found - step
            fine = score_field(anchor, other(fine_start, step * 2 + run))
            if fine:
                found = fine_start + max(range(len(fine)), key=lambda index: fine[index])
        return found - position

    # The offset is one number for the whole film, so the wide search only has
    # to happen once. Doing it at every position is what made this unusable on
    # real material: one percent of a feature is 2093 frames either side, and
    # nine wide searches is tens of thousands of 4K frames decoded per source.
    # The rest confirm it within CONFIRM_REACH of where the first one landed,
    # which is what catches a position that fell in a shot the film repeats.
    first = None
    searched = 0
    for index, position in enumerate(positions):
        first = search(position, 0, window)
        if first is not None:
            searched = index
            break
    if first is None:
        return FrameOffset(name, 0, 0, 0, window)

    approximate = first
    votes = [approximate]
    for position in positions[searched + 1 :]:
        confirmed = search(position, approximate, CONFIRM_REACH)
        if confirmed is not None:
            votes.append(confirmed)

    offset = int(_median([float(vote) for vote in votes]))
    return FrameOffset(name, offset, votes.count(offset), len(votes), window)


def suggested_trims(offsets: Sequence[int], trims: Sequence[int]) -> list[int]:
    """Trims that line every source up, none of them negative.

    Both sequences cover **every** source including the reference, whose own
    offset is zero by definition. The reference has to be in the list because
    it is the one that moves when another source turns out to play *earlier*:
    a negative trim is not the answer there, trimming the reference instead is.
    ``trim = -5`` is valid Python -- ``clip[-5:]`` takes the last five frames
    of the film -- so handing someone a negative number to paste would be
    handing them a comparison of the closing credits.

    The measurement is a residual, because the sources were prepared with
    their existing trims already applied, so the offsets are added to what is
    already there rather than replacing it.
    """
    if not offsets:
        return []
    wanted = [trim + offset for trim, offset in zip(trims, offsets)]
    shift = min(wanted)
    return [value - shift for value in wanted]
