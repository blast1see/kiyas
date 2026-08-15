"""What an audio track claims to be.

Claims, deliberately: everything here is read out of the container. What the
audio *is* -- its real bit depth, whether it clips, whether the surround
channels contain anything -- is measured in :mod:`kiyas.audio.analysis`, and
the two disagreeing is one of the more interesting things a comparison can
show.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..media import binaries
from ..media.probe import ProbeError, run_ffprobe

#: MediaInfo on a long track is fast; a hang means something else is wrong.
_MEDIAINFO_TIMEOUT = 120.0


@dataclass(frozen=True, slots=True)
class AudioTrack:
    """One audio stream, as the file describes it."""

    path: Path
    index: int
    codec: str
    profile: str | None
    channels: int
    channel_layout: str
    sample_rate: int
    #: What the container says. Lossy codecs report nothing meaningful here,
    #: which is why it is optional rather than defaulted to 16.
    bit_depth: int | None
    bitrate: int | None
    duration: float
    language: str | None
    title: str | None
    #: Fields MediaInfo knows about and ffprobe does not, notably dialnorm.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """A short human name for this track, for a column heading."""
        parts = [self.codec.upper()]
        if self.channel_layout:
            parts.append(self.channel_layout)
        if self.language:
            parts.append(self.language)
        return " ".join(parts)

    @property
    def is_lossless(self) -> bool:
        return self.codec.lower() in {"flac", "truehd", "mlp", "pcm_s16le", "pcm_s24le", "alac"}


def _int_or_none(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def probe_track(
    path: str | Path, *, index: int = 0, ffprobe: str | Path | None = None
) -> AudioTrack:
    """Inspect audio stream ``index`` of ``path``."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ProbeError(f"no such file: {path}")

    binary = binaries.require_binary("ffprobe", ffprobe)
    data = run_ffprobe(
        [
            "-v",
            "error",
            "-select_streams",
            f"a:{index}",
            "-show_streams",
            "-show_format",
            "-print_format",
            "json",
            str(path),
        ],  # fmt: skip
        ffprobe=binary,
    )

    streams = data.get("streams") or []
    if not streams:
        raise ProbeError(
            f"{path.name} has no audio stream at index {index}. "
            f"Use --track to pick a different one."
        )
    stream = streams[0]
    tags = {str(k).lower(): str(v) for k, v in (stream.get("tags") or {}).items()}

    duration = 0.0
    for candidate in (stream.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break

    sample_rate = _int_or_none(stream.get("sample_rate")) or 48000
    channels = _int_or_none(stream.get("channels")) or 2

    return AudioTrack(
        path=path,
        index=index,
        codec=str(stream.get("codec_name") or "unknown"),
        profile=stream.get("profile") or None,
        channels=channels,
        channel_layout=str(stream.get("channel_layout") or f"{channels}ch"),
        sample_rate=sample_rate,
        bit_depth=_int_or_none(stream.get("bits_per_raw_sample"))
        or _int_or_none(stream.get("bits_per_sample")),
        bitrate=_int_or_none(stream.get("bit_rate")),
        duration=duration,
        language=tags.get("language"),
        title=tags.get("title"),
    )


def count_tracks(path: str | Path, *, ffprobe: str | Path | None = None) -> int:
    """How many audio streams the file has."""
    binary = binaries.require_binary("ffprobe", ffprobe)
    data = run_ffprobe(
        [
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-print_format",
            "json",
            str(Path(path).expanduser()),
        ],  # fmt: skip
        ffprobe=binary,
    )
    return len(data.get("streams") or [])


#: MediaInfo fields worth having that ffprobe does not report.
#:
#: Bit depth is deliberately absent: ffprobe already reports what the file
#: claims, and where the two disagree the *measured* depth is the answer that
#: matters. Two rows saying "declared bit depth" is just confusing.
#:
#: ``dialnorm`` is the one that matters: it is a level the decoder applies at
#: playback, so two tracks with the same waveform can sound several dB apart.
#: Comparing loudness without it produces a confident wrong answer.
_MEDIAINFO_FIELDS = {
    "Dialnorm": "dialnorm",
    "Dialnorm_Average": "dialnorm average",
    "Compression_Mode": "compression",
    "Format_Settings_Endianness": "endianness",
    "Format_AdditionalFeatures": "format features",
    "ServiceKind/String": "service kind",
    "Encoded_Library": "encoder",
}


def mediainfo_extras(
    path: str | Path, *, index: int = 0, mediainfo: str | Path | None = None
) -> dict[str, str]:
    """Extra fields from MediaInfo, or an empty dict if it is not installed.

    Never an error. MediaInfo is a nice-to-have: without it the specification
    table is shorter, which is a smaller problem than refusing to run.
    """
    binary = binaries.find_binary("mediainfo", mediainfo)
    if binary is None:
        return {}
    try:
        proc = subprocess.run(  # noqa: S603
            [str(binary), "--Output=JSON", str(Path(path).expanduser())],
            capture_output=True,
            text=True,
            timeout=_MEDIAINFO_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
        payload = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}

    tracks = ((payload.get("media") or {}).get("track")) or []
    audio_tracks = [t for t in tracks if str(t.get("@type", "")).lower() == "audio"]
    if index >= len(audio_tracks):
        return {}

    track = audio_tracks[index]
    extras: dict[str, str] = {}
    for key, label in _MEDIAINFO_FIELDS.items():
        value = track.get(key)
        if value not in (None, "", []):
            extras[label] = str(value)
    return extras
