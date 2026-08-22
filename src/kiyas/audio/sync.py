"""How far apart two tracks are.

Comparing two audio tracks that are not aligned measures the misalignment and
nothing else, so this runs first and its answer is reported next to everything
else. A hundred milliseconds is inaudible as a delay and obvious as a
difference in every waveform and spectrogram you then look at.

**AudioSyncTool does this properly and is used when it is installed.** It has a
drift fit and rate-mismatch detection that this module does not attempt. What
is here is the fallback: one GCC-PHAT correlation over a long window, which is
enough to answer "are these the same edit, and by how much are they out" and
not enough to answer "does one of them run slow".

Two things carried over from AudioSyncTool, both learned the hard way:

**The sign convention is stated, not implied.** ``offset_ms > 0`` means the
second track plays *later* than the first -- delay it by that much less, or
advance it. Getting this backwards produces a correction in the wrong direction
and a result twice as wrong as doing nothing.

**A confident wrong answer is the failure mode.** Repetitive music correlates
strongly at the wrong period, and the correlation peak looks just as sharp
there as at the right one. The peak-to-floor ratio is reported with the offset
so a weak match can be seen for what it is.

**One number is less certain than it looks.** The same real pair measured
+10571, +10524 and +10527 ms over five-, ten- and twenty-minute windows. A
single correlation assumes the two tracks are a constant distance apart, and
across a whole film they often are not -- which is the question AudioSyncTool's
drift fit answers and this does not. Treat the last few milliseconds as noise,
and install AudioSyncTool when they matter.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..media import binaries
from .analysis import AnalysisError
from .probe import AudioTrack

#: Correlation is done on a mono downmix at this rate.
#:
#: 4 kHz, which is lower than it looks. Alignment lives in the envelope, not
#: the timbre, and the lag resolution this leaves -- 0.25 ms -- is an order of
#: magnitude finer than the measurement's real spread. Measured on a real dub
#: pair: 4 kHz and 8 kHz agreed to 0.1 ms, 4 kHz had the *higher* confidence
#: (156 against 132), and it halves the transform.
SYNC_RATE = 4000

#: Longest stretch correlated, in seconds.
#:
#: Twenty minutes. Measured on the same pair, confidence by window: 5 minutes
#: 45, 10 minutes 108, 20 minutes 156, 30 minutes 81. It falls off past twenty
#: because the extra material reaches into parts the two versions do not share.
MAX_WINDOW = 20 * 60

#: Below this peak-to-floor ratio the answer is a guess, and says so.
WEAK_MATCH = 4.0

_DECODE_TIMEOUT = 1800.0


@dataclass(frozen=True, slots=True)
class Offset:
    """How far the second track is from the first."""

    milliseconds: float
    confidence: float
    method: str

    @property
    def is_weak(self) -> bool:
        return self.confidence < WEAK_MATCH

    @property
    def summary(self) -> str:
        direction = "later" if self.milliseconds > 0 else "earlier"
        text = f"{abs(self.milliseconds):.1f} ms {direction} ({self.method})"
        if self.is_weak:
            text += f" -- weak match, peak-to-floor {self.confidence:.1f}"
        return text


def _mono(track: AudioTrack, *, ffmpeg: Path, seconds: float, start: float):
    """Decode a stretch of ``track`` to mono float at :data:`SYNC_RATE`."""
    import numpy as np

    args = [
        str(ffmpeg), "-v", "error", "-nostdin",
        "-ss", f"{max(0.0, start):.3f}",
        "-t", f"{seconds:.3f}",
        "-i", str(track.path),
        "-map", f"0:a:{track.index}",
        "-ac", "1",
        "-ar", str(SYNC_RATE),
        "-f", "f32le", "-acodec", "pcm_f32le", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            timeout=_DECODE_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=binaries.no_window_flag(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AnalysisError(f"could not decode {track.path.name} for sync: {exc}") from exc
    if not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise AnalysisError(
            f"no audio decoded from {track.path.name}: {detail[-1] if detail else 'empty output'}"
        )
    # Kept in single precision; see _gcc_phat for why that matters here.
    return np.frombuffer(proc.stdout, dtype="<f4").copy()


def _gcc_phat(first, second):
    """Cross-correlate with the phase transform; return (lag, peak, floor).

    Plain cross-correlation is dominated by whatever is loudest, so on a film
    it locks onto the music rather than the edit. Dividing by the magnitude
    keeps only the phase, which is where the timing lives, and turns the result
    into a sharp spike at the true lag instead of a broad hill.

    ``scipy.fft`` rather than ``numpy.fft``, and single precision: numpy always
    computes in double whatever it is handed, so a float32 input still
    allocates complex128. scipy keeps the precision it is given, which halves
    the largest arrays here for an answer that is identical to the tenth of a
    millisecond -- the lag is an array index, not a float being accumulated.
    """
    import numpy as np
    from scipy import fft

    size = 1 << int(np.ceil(np.log2(len(first) + len(second))))
    spectrum = fft.rfft(first, size) * np.conj(fft.rfft(second, size))
    magnitude = np.abs(spectrum)
    magnitude[magnitude < 1e-12] = 1e-12
    correlation = fft.irfft(spectrum / magnitude, size)
    correlation = np.concatenate((correlation[-(size // 2) :], correlation[: size // 2]))

    index = int(np.argmax(np.abs(correlation)))
    peak = float(np.abs(correlation[index]))
    # The floor is the typical height everywhere else. A real match stands well
    # clear of it; a lock onto a repeated bar does not.
    floor = float(np.median(np.abs(correlation))) or 1e-12
    return index - size // 2, peak, floor


def _fallback(first: AudioTrack, second: AudioTrack, *, ffmpeg: Path) -> Offset:
    shortest = min(first.duration or MAX_WINDOW, second.duration or MAX_WINDOW)
    window = min(MAX_WINDOW, shortest)
    # From the middle. The start of a film is logos and silence, and the end is
    # credits over a music bed that repeats -- both are where correlation goes
    # wrong.
    start = max(0.0, (shortest - window) / 2)

    left = _mono(first, ffmpeg=ffmpeg, seconds=window, start=start)
    right = _mono(second, ffmpeg=ffmpeg, seconds=window, start=start)
    usable = min(len(left), len(right))
    if usable < SYNC_RATE:
        raise AnalysisError("not enough audio decoded to measure an offset")

    lag, peak, floor = _gcc_phat(left[:usable], right[:usable])
    return Offset(
        milliseconds=-lag * 1000.0 / SYNC_RATE,
        confidence=peak / floor,
        method="GCC-PHAT",
    )


def measure(first: AudioTrack, second: AudioTrack, *, ffmpeg: str | Path | None = None) -> Offset:
    """Measure how far ``second`` is from ``first``.

    Uses AudioSyncTool when it is importable and falls back to a single
    correlation otherwise. The method is reported either way, because the two
    do not answer quite the same question and the difference matters when the
    numbers are argued over.
    """
    binary = binaries.require_binary("ffmpeg", ffmpeg)
    try:
        return _with_audio_sync_tool(first, second)
    except ImportError:
        return _fallback(first, second, ffmpeg=binary)


def _with_audio_sync_tool(first: AudioTrack, second: AudioTrack) -> Offset:
    """Delegate to AudioSyncTool, which already solves this properly."""
    from audio_sync.core.analyzer import analyze  # noqa: PLC0415 - optional dependency

    result = analyze(str(first.path), str(second.path))
    return Offset(
        milliseconds=float(getattr(result, "delay_ms", 0.0)),
        confidence=float(getattr(result, "confidence", 0.0)),
        method="AudioSyncTool",
    )
