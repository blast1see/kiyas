"""Measuring what is actually in an audio track.

**One pass, decoded to float.** A two-hour 5.1 track is about eight gigabytes of
samples; holding it is out of the question and decoding it twice is a minute
wasted, so everything below is accumulated chunk by chunk from a single ffmpeg
pipe: peak, RMS, clipping, the waveform envelope, the real bit depth and the
frequency response.

**Float, not integers, and that is not the obvious choice.** Decoding to 32-bit
integers makes the bit-depth measurement trivial -- count trailing zero bits --
but it also clamps, which turns every decoder overshoot in a lossy track into
something indistinguishable from real clipping. Floats keep the overshoots, and
the bit depth survives anyway: a lossless 16-bit source decodes to exact
multiples of 1/32768, and scaling by 2^31 lands on integers with sixteen
trailing zeros. Float32 carries a 24-bit mantissa, so 24-bit sources are exact
too.

**The frequency response is the whole track, not a sample of it.** Averaging
periodograms as the chunks go past costs almost nothing on top of a decode that
is happening anyway, and "I looked at twenty seconds of it" is a weaker claim
than it needs to be.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..media import binaries
from .probe import AudioTrack

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

#: Waveform windows across the whole track. Enough that a 90-minute film shows
#: individual scenes, few enough that the picture is not one grey block.
ENVELOPE_WINDOWS = 2000

#: FFT size for the frequency response. At 48 kHz this is a 43 ms window and
#: about 23 Hz per bin: fine enough to place a lossy codec's low-pass to within
#: a few hundred Hz, which is what the curve is read for.
FFT_SIZE = 2048

#: A sample at or above this is treated as clipped. Not 1.0: a sample that
#: reaches exactly full scale is normal, and a *run* of them is what clipping
#: actually looks like, which is why runs are counted separately.
CLIP_LEVEL = 0.999

#: How many consecutive full-scale samples count as a clipped region. One
#: sample at full scale happens in any loud master; three in a row does not
#: happen without something having run out of headroom.
CLIP_RUN = 3

#: Below this a channel is treated as empty. -90 dBFS is under the noise floor
#: of 16-bit audio, so anything above it is content rather than dither.
SILENT_DBFS = -90.0

#: Two channels count as duplicates when the energy of their difference is
#: below this fraction of the energy they carry. Not zero: a lossy codec
#: decodes two identical input channels to very slightly different output, so
#: an exact test would report nothing on the files where this matters most.
DUPLICATE_ENERGY = 1e-6

#: Decoding is bounded by disk and CPU, not by anything that should hang.
_DECODE_TIMEOUT = 3600.0


class AnalysisError(RuntimeError):
    """Raised when a track cannot be analysed."""


@dataclass(slots=True)
class AudioAnalysis:
    """Everything measured from one track."""

    track: AudioTrack
    channel_names: list[str]
    #: dBFS per channel, and overall.
    peak_dbfs: list[float]
    rms_dbfs: list[float]
    #: Runs of consecutive full-scale samples, per channel.
    clipped_runs: list[int]
    #: Where clipping starts, in seconds. A *sample* for marking the waveform,
    #: not a full map: collection stops after a few hundred, so on a track that
    #: clips throughout these are drawn from the early part of it.
    #: :attr:`clipped_runs` is the complete count.
    clip_positions: list[float]
    #: Measured, not declared. ``None`` when the answer would be meaningless,
    #: which is any lossy codec: those decode to arbitrary float values that
    #: use every bit whatever the source was.
    real_bit_depth: int | None
    #: ``(windows, channels, 2)`` of min and max, for drawing the waveform.
    envelope: np.ndarray
    #: ``(channels, bins)`` power spectral density, and the matching frequencies.
    spectrum: np.ndarray
    frequencies: np.ndarray
    #: Pairs of channel names carrying the same audio.
    duplicate_channels: list[tuple[str, str]]
    frames: int
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.frames / self.track.sample_rate if self.track.sample_rate else 0.0

    @property
    def overall_peak_dbfs(self) -> float:
        return max(self.peak_dbfs) if self.peak_dbfs else -float("inf")

    @property
    def silent_channels(self) -> list[str]:
        return [
            name
            for name, peak in zip(self.channel_names, self.peak_dbfs, strict=True)
            if peak < SILENT_DBFS
        ]

    @property
    def clipped(self) -> int:
        return sum(self.clipped_runs)


#: Channel names by layout size, in ffmpeg's channel order. Used for labelling
#: only; the analysis itself never assumes what a channel contains.
_LAYOUTS: dict[int, tuple[str, ...]] = {
    1: ("Mono",),
    2: ("L", "R"),
    3: ("L", "R", "LFE"),
    4: ("L", "R", "Ls", "Rs"),
    5: ("L", "R", "C", "Ls", "Rs"),
    6: ("L", "R", "C", "LFE", "Ls", "Rs"),
    8: ("L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb"),
}


def channel_names(channels: int) -> list[str]:
    names = _LAYOUTS.get(channels)
    return list(names) if names else [f"ch{n + 1}" for n in range(channels)]


def _decoder(track: AudioTrack, ffmpeg: Path) -> subprocess.Popen:
    """ffmpeg writing raw float samples to a pipe."""
    args = [
        str(ffmpeg), "-v", "error", "-nostdin",
        "-i", str(track.path),
        "-map", f"0:a:{track.index}",
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-",
    ]  # fmt: skip
    try:
        return subprocess.Popen(  # noqa: S603 - path resolved, args built here
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AnalysisError(f"could not start ffmpeg: {exc}") from exc


def _trailing_zeros(values: np.ndarray) -> int:
    """Fewest trailing zero bits among non-zero samples scaled to 32 bits.

    The measurement that says a 24-bit file holds 16-bit content. Samples are
    scaled by 2^31 and rounded; a source quantised to N bits lands on multiples
    of 2^(32-N), so the smallest number of trailing zeros across the track
    gives N back.
    """
    import numpy as np

    scaled = np.rint(values.astype(np.float64) * (1 << 31)).astype(np.int64)
    scaled = scaled[scaled != 0]
    if scaled.size == 0:
        return 32
    # Two's-complement trailing zeros: x & -x isolates the lowest set bit.
    lowest = np.bitwise_and(scaled, -scaled)
    return int(np.log2(np.min(np.abs(lowest))))


def analyse(
    track: AudioTrack,
    *,
    ffmpeg: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> AudioAnalysis:
    """Decode ``track`` once and measure everything from that pass."""
    try:
        import numpy as np
        from scipy import signal
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise AnalysisError(
            "audio analysis needs numpy and scipy. Run 'pip install kiyas[audio]'."
        ) from exc

    binary = binaries.require_binary("ffmpeg", ffmpeg)
    channels = track.channels
    names = channel_names(channels)

    expected_frames = int(track.duration * track.sample_rate) if track.duration else 0
    window = max(1, expected_frames // ENVELOPE_WINDOWS) if expected_frames else 1 << 14
    # Read whole windows at a time so an envelope window never straddles two
    # reads; the alternative is a carry buffer whose only job is to be a bug.
    frames_per_read = window * max(1, (1 << 20) // (window * channels * 4) or 1)

    envelope: list[np.ndarray] = []
    peak = np.zeros(channels, dtype=np.float64)
    square_sum = np.zeros(channels, dtype=np.float64)
    clip_runs = np.zeros(channels, dtype=np.int64)
    clip_positions: list[float] = []
    psd_sum = np.zeros((channels, FFT_SIZE // 2 + 1), dtype=np.float64)
    psd_blocks = 0
    frequencies = np.fft.rfftfreq(FFT_SIZE, 1 / track.sample_rate)
    smallest_bit = 32
    total_frames = 0
    # Energy of the difference between every pair of channels. Two channels
    # that are copies of each other is what a "5.1" upmix of a stereo master
    # looks like, and a "2.0" dub that is really mono duplicated -- seen on a
    # real streaming dub, where the two frequency-response curves lay exactly
    # on top of each other and the plot alone could not say why.
    pairs = [(a, b) for a in range(channels) for b in range(a + 1, channels)]
    pair_energy = np.zeros(len(pairs), dtype=np.float64)

    process = _decoder(track, binary)
    frame_bytes = channels * 4
    read_bytes = frames_per_read * frame_bytes
    carry = b""
    try:
        while True:
            raw = carry + process.stdout.read(read_bytes)
            if not raw:
                break
            # Keep any partial frame for the next read. A pipe hands over
            # whatever bytes happen to be there, and dropping the remainder
            # would start the next read half a frame late -- which does not
            # fail, it *rotates the channels* from that point on. Mono files
            # cannot show the bug, which is exactly how it survives review.
            usable = (len(raw) // frame_bytes) * frame_bytes
            carry, raw = raw[usable:], raw[:usable]
            if not raw:
                continue
            block = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)

            magnitude = np.abs(block)
            peak = np.maximum(peak, magnitude.max(axis=0))
            wide = block.astype(np.float64)
            square_sum += np.square(wide).sum(axis=0)
            for index, (left, right) in enumerate(pairs):
                pair_energy[index] += np.square(wide[:, left] - wide[:, right]).sum()

            # Clipping: runs of consecutive full-scale samples, per channel.
            over = magnitude >= CLIP_LEVEL
            if over.any():
                for channel in range(channels):
                    starts = _run_starts(over[:, channel], CLIP_RUN)
                    clip_runs[channel] += len(starts)
                    if len(clip_positions) < 500:
                        clip_positions.extend(
                            (total_frames + int(start)) / track.sample_rate for start in starts[:20]
                        )

            # Waveform envelope, one min/max pair per window.
            whole = (block.shape[0] // window) * window
            if whole:
                shaped = block[:whole].reshape(-1, window, channels)
                envelope.append(np.stack([shaped.min(axis=1), shaped.max(axis=1)], axis=-1))

            if track.is_lossless:
                smallest_bit = min(smallest_bit, _trailing_zeros(block))

            # Frequency response, accumulated rather than sampled.
            if block.shape[0] >= FFT_SIZE:
                _, power = signal.welch(
                    block,
                    fs=track.sample_rate,
                    nperseg=FFT_SIZE,
                    axis=0,
                    scaling="density",
                )
                psd_sum += power.T
                psd_blocks += 1

            total_frames += block.shape[0]
            if progress and expected_frames:
                done = min(100, int(100 * total_frames / expected_frames))
                progress(f"analysing {track.path.name} {done}%")

        stderr = (process.stderr.read() or b"").decode("utf-8", "replace")
        if process.wait(timeout=_DECODE_TIMEOUT) != 0 and total_frames == 0:
            detail = stderr.strip().splitlines()
            raise AnalysisError(
                f"ffmpeg could not decode {track.path.name}: "
                f"{detail[-1] if detail else 'no output'}"
            )
    finally:
        if process.poll() is None:  # pragma: no cover - only on an exception
            process.kill()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    if total_frames == 0:
        raise AnalysisError(f"{track.path.name} decoded to no audio at all")

    notes: list[str] = []
    real_depth: int | None = None
    if track.is_lossless:
        real_depth = 32 - smallest_bit
        if track.bit_depth and real_depth < track.bit_depth:
            notes.append(
                f"declared {track.bit_depth}-bit but only {real_depth} bits are used; "
                f"the extra bits are padding, not detail"
            )
    else:
        notes.append(
            f"real bit depth is not measurable for {track.codec}: a lossy decoder produces "
            f"float output that uses every bit whatever the source was"
        )

    with np.errstate(divide="ignore"):
        peak_dbfs = (20 * np.log10(np.maximum(peak, 1e-12))).tolist()
        rms = np.sqrt(square_sum / max(1, total_frames))
        rms_dbfs = (20 * np.log10(np.maximum(rms, 1e-12))).tolist()
        spectrum = 10 * np.log10(np.maximum(psd_sum / max(1, psd_blocks), 1e-20))

    # Two channels are duplicates when the energy of their difference is
    # negligible against the energy they carry. Relative, not an absolute
    # threshold: quiet channels would otherwise all look identical.
    duplicates: list[tuple[str, str]] = []
    for index, (left, right) in enumerate(pairs):
        reference = square_sum[left] + square_sum[right]
        if reference > 0 and pair_energy[index] / reference < DUPLICATE_ENERGY:
            duplicates.append((names[left], names[right]))

    analysis = AudioAnalysis(
        track=track,
        channel_names=names,
        peak_dbfs=peak_dbfs,
        rms_dbfs=rms_dbfs,
        clipped_runs=clip_runs.tolist(),
        clip_positions=clip_positions,
        real_bit_depth=real_depth,
        envelope=np.concatenate(envelope, axis=0)
        if envelope
        else np.zeros((0, channels, 2), dtype=np.float32),
        spectrum=spectrum,
        frequencies=frequencies,
        duplicate_channels=duplicates,
        frames=total_frames,
        notes=notes,
    )
    if analysis.silent_channels:
        notes.append(
            f"silent channel(s): {', '.join(analysis.silent_channels)}. A track whose surrounds "
            f"contain nothing is usually an upmix rather than a real multichannel master."
        )
    if duplicates:
        pairs_text = ", ".join(f"{left}={right}" for left, right in duplicates)
        notes.append(
            f"identical channel(s): {pairs_text}. The track is carrying fewer distinct channels "
            f"than it declares, which is what a duplicated mono dub or a stereo upmix looks like."
        )
    return analysis


def _run_starts(flags: np.ndarray, minimum: int) -> list[int]:
    """Indices where a run of at least ``minimum`` True values begins."""
    import numpy as np

    if not flags.any():
        return []
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2]
    lengths = ends - starts
    return starts[lengths >= minimum].tolist()
