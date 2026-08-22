"""Reading what a media file actually is, via ffprobe.

Two things here are easy to get wrong and expensive to debug later.

**Frame rate is a fraction, not a float.** 24000/1001 is not 23.976. Rounding
it and then multiplying by a two-hour runtime drifts by several frames, which
is exactly the error a comparison is supposed to expose.

**Frame counts from a container are estimates.** ``nb_frames`` is absent from
most Matroska files, so the count is derived from duration times frame rate and
can be off by a frame or two at the tail. When VapourSynth is the engine its
``clip.num_frames`` is exact and supersedes this; :attr:`VideoInfo.exact` says
which one you are holding.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path

from . import binaries

#: ffprobe on a two-hour remux over a network share is not instant, but it
#: should never be minutes either.
_PROBE_TIMEOUT = 120.0


class ProbeError(RuntimeError):
    """Raised when a file cannot be inspected."""


class HdrFormat(Enum):
    SDR = "sdr"
    HDR10 = "hdr10"
    HLG = "hlg"
    DOVI = "dovi"

    @property
    def is_hdr(self) -> bool:
        return self is not HdrFormat.SDR


#: ffprobe spellings that mean "this is PQ". ffmpeg has used both over the
#: years and still emits either depending on how the file was tagged.
_PQ_TRANSFERS = {"smpte2084", "smpte-st-2084"}
_HLG_TRANSFERS = {"arib-std-b67", "arib_std_b67"}
_BT2020_PRIMARIES = {"bt2020"}


@dataclass(frozen=True, slots=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    duration: float
    frame_count: int
    exact: bool
    codec: str
    pix_fmt: str
    bit_depth: int
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    dovi_profile: int | None
    dovi_el_present: bool = False

    @property
    def hdr_format(self) -> HdrFormat:
        """What kind of HDR this is, if any.

        Dolby Vision wins over the transfer characteristics because a DoVi
        profile 5 file carries no usable HDR10 layer at all -- tonemapping it
        as HDR10 produces the green/purple cast that makes those screenshots
        useless.
        """
        if self.dovi_profile is not None:
            return HdrFormat.DOVI
        transfer = (self.color_transfer or "").lower()
        if transfer in _PQ_TRANSFERS:
            return HdrFormat.HDR10
        if transfer in _HLG_TRANSFERS:
            return HdrFormat.HLG
        # A file tagged BT.2020 without a PQ/HLG transfer is wide-gamut SDR.
        # Treating it as HDR would crush it; leave it alone.
        return HdrFormat.SDR

    @property
    def has_enhancement_layer(self) -> bool:
        """Whether this is a profile 7 file whose picture is split over two layers.

        Only profile 7 carries a picture-bearing enhancement layer. Profile 8
        sets the flag to 0 and profile 5 has no second layer to speak of, so
        asking for the profile as well as the flag keeps a mis-tagged file from
        sending the engine looking for a layer that is not there.
        """
        return self.dovi_profile == 7 and self.dovi_el_present

    @property
    def is_wide_gamut(self) -> bool:
        return (self.color_primaries or "").lower() in _BT2020_PRIMARIES

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


def run_ffprobe(args: list[str], *, ffprobe: Path) -> dict:
    try:
        proc = subprocess.run(  # noqa: S603
            [str(ffprobe), *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=binaries.no_window_flag(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {_PROBE_TIMEOUT:.0f}s") from exc
    except OSError as exc:
        raise ProbeError(f"could not run ffprobe: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise ProbeError(detail[-1] if detail else f"ffprobe exited {proc.returncode}")

    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned output that is not JSON: {exc}") from exc


def _parse_fraction(text: str | None, default: Fraction) -> Fraction:
    """Parse ffprobe's ``num/den`` rate strings.

    ``0/0`` shows up for streams whose rate ffprobe could not determine, and
    must not become a ZeroDivisionError halfway through a comparison.
    """
    if not text:
        return default
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return default
    return value if value > 0 else default


def _bit_depth(stream: dict) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        raw = stream.get(key)
        if raw:
            try:
                depth = int(raw)
            except (TypeError, ValueError):
                continue
            if depth > 0:
                return depth
    # ffprobe omits the field for common formats; the pixel format name carries
    # it instead (yuv420p10le -> 10).
    pix_fmt = str(stream.get("pix_fmt", ""))
    for depth in (16, 14, 12, 10):
        if f"p{depth}" in pix_fmt:
            return depth
    return 8


def _dovi_profile(stream: dict) -> int | None:
    """Read the Dolby Vision profile from the stream's configuration record."""
    for side_data in stream.get("side_data_list", []) or []:
        if "dv_profile" in side_data:
            try:
                return int(side_data["dv_profile"])
            except (TypeError, ValueError):
                return None
    return None


def _dovi_el_present(stream: dict) -> bool:
    """Whether the stream carries a Dolby Vision enhancement layer.

    The same configuration record that holds the profile also holds
    ``el_present_flag``, so this costs nothing beyond reading a second key.
    Measured on two files from the same film: the profile 7 disc remux reports
    ``dv_profile=7, el_present_flag=1``, the profile 8.1 WEB-DL reports
    ``dv_profile=8, el_present_flag=0``.

    The flag is set for both minimal and full enhancement layers. Telling those
    apart needs the RPU itself, and nothing here needs to: composing a minimal
    layer is a no-op on the picture rather than a wrong answer.
    """
    for side_data in stream.get("side_data_list", []) or []:
        if "el_present_flag" in side_data:
            try:
                return bool(int(side_data["el_present_flag"]))
            except (TypeError, ValueError):
                return False
    return False


def probe(path: str | Path, *, ffprobe: str | Path | None = None) -> VideoInfo:
    """Inspect the first video stream of ``path``."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise ProbeError(f"no such file: {path}")

    binary = binaries.require_binary("ffprobe", ffprobe)
    data = run_ffprobe(
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-show_format",
            "-print_format",
            "json",
            str(path),
        ],
        ffprobe=binary,
    )

    streams = data.get("streams") or []
    if not streams:
        raise ProbeError(f"{path.name} has no video stream")
    stream = streams[0]

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ProbeError(f"{path.name}: video stream reports no usable dimensions")

    # avg_frame_rate is what the file plays at; r_frame_rate is the base rate,
    # which is doubled for telecined and interlaced content. Frame indices come
    # from the former.
    fps = _parse_fraction(stream.get("avg_frame_rate"), Fraction(24000, 1001))
    if fps <= 0:
        fps = _parse_fraction(stream.get("r_frame_rate"), Fraction(24000, 1001))

    duration = 0.0
    for candidate in (stream.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break

    exact = False
    frame_count = 0
    raw_count = stream.get("nb_frames")
    try:
        frame_count = int(raw_count)
        exact = frame_count > 0
    except (TypeError, ValueError):
        frame_count = 0
    if frame_count <= 0:
        frame_count = int(duration * fps) if duration > 0 else 0
        exact = False

    return VideoInfo(
        path=path,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        frame_count=frame_count,
        exact=exact,
        codec=str(stream.get("codec_name") or "unknown"),
        pix_fmt=str(stream.get("pix_fmt") or "unknown"),
        bit_depth=_bit_depth(stream),
        color_primaries=stream.get("color_primaries"),
        color_transfer=stream.get("color_transfer"),
        color_space=stream.get("color_space"),
        dovi_profile=_dovi_profile(stream),
        dovi_el_present=_dovi_el_present(stream),
    )
