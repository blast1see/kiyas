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
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..media import binaries
from .analysis import AnalysisError
from .probe import AudioTrack

#: Correlation is done on a mono downmix at this rate. 8 kHz keeps everything
#: that matters for alignment -- the envelope, not the timbre -- and makes the
#: transform of a twenty-minute window cheap.
SYNC_RATE = 8000

#: Longest stretch correlated, in seconds. Twenty minutes is far more than
#: enough to lock on, and bounds the cost on a feature.
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
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AnalysisError(f"could not decode {track.path.name} for sync: {exc}") from exc
    if not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise AnalysisError(
            f"no audio decoded from {track.path.name}: {detail[-1] if detail else 'empty output'}"
        )
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float64)


def _gcc_phat(first, second):
    """Cross-correlate with the phase transform; return (lag, peak, floor).

    Plain cross-correlation is dominated by whatever is loudest, so on a film
    it locks onto the music rather than the edit. Dividing by the magnitude
    keeps only the phase, which is where the timing lives, and turns the result
    into a sharp spike at the true lag instead of a broad hill.
    """
    import numpy as np

    size = 1 << int(np.ceil(np.log2(len(first) + len(second))))
    spectrum = np.fft.rfft(first, size) * np.conj(np.fft.rfft(second, size))
    magnitude = np.abs(spectrum)
    magnitude[magnitude < 1e-12] = 1e-12
    correlation = np.fft.irfft(spectrum / magnitude, size)
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
