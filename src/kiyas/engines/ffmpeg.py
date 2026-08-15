"""The ffmpeg engine: always available, good enough, not the best.

Its reason to exist is that ffmpeg is already on the machine. No 400 MB
download, no plugin stack, nothing to configure. For a quick look at two files
that is the right trade.

Where it is worse than VapourSynth, and why:

**Seeking.** Frames are addressed by timestamp. ``-ss`` before ``-i`` seeks to
the preceding keyframe and decodes forward discarding frames, which is accurate
but relies on the container's timestamps being honest. Variable frame rate or
broken timestamps will put you on a neighbouring frame, and across two sources
that is a desync you cannot see in the output.

**Tonemapping.** The zscale chain below is the standard CPU HDR to SDR path. It
is static: no per-scene metadata, so no HDR10+ and no Dolby Vision. libplacebo
would be better but needs a Vulkan device, which cannot be assumed.

Each frame is one subprocess. That is fine for a dozen screenshots and wrong
for a thousand.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from fractions import Fraction
from pathlib import Path

from ..config import Source, Tonemap
from ..media import binaries
from ..media.probe import HdrFormat, VideoInfo, probe
from .base import ACTIVE_AREA, EngineError, RenderSettings

#: Decoding a single frame from a keyframe boundary; a slow disk and a long GOP
#: can make this take a while, but not this long.
_FRAME_TIMEOUT = 180.0

#: Fraction of an estimated frame count treated as unusable at the tail.
#:
#: Relative, not a number of seconds. The measured overshoot on a feature was
#: 0.08% (125 frames of 157013), and a percentage covers that while staying
#: proportionate on short material -- a fixed ten-second margin was tried first
#: and erased four-second clips entirely.
_ESTIMATE_MARGIN = 0.002

#: ...but never fewer than this many frames, so a clip too short for the
#: percentage to round to anything still gets some slack.
_ESTIMATE_MARGIN_MIN = 2

#: The standard CPU HDR-to-SDR chain. Linearise, work in float, tonemap, then
#: come back to limited-range BT.709. ``npl=100`` is the assumed display peak;
#: ``desat=0`` disables the desaturation ffmpeg applies to highlights by
#: default, which otherwise washes out exactly the specular detail a
#: comparison is looking at.
_TONEMAP_CHAIN = (
    "zscale=transfer=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=primaries=bt709,"
    "tonemap=tonemap=mobius:desat=0,"
    "zscale=transfer=bt709:matrix=bt709:range=tv,"
    "format=yuv420p"
)


class FfmpegSource:
    def __init__(
        self,
        source: Source,
        info: VideoInfo,
        ffmpeg: Path,
        ffprobe: Path,
        filters: list[str],
        target_fps: Fraction | None,
    ):
        self.name = source.name
        self.info = info
        self.assumptions: list[str] = []
        self._source = source
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._filters = filters
        self._pict_cache: dict[int, str] = {}
        self._scanned: list[tuple[int, int]] = []
        self._pict_types_usable: bool | None = None

        self.fps = target_fps or info.fps
        # Trim shifts the whole index space, exactly as it does in VapourSynth,
        # so frame 0 here means frame `trim` in the file.
        #
        # The margin is because a container frame count is usually derived from
        # duration times frame rate, and it overshoots. Measured on a retail
        # Blu-ray remux: ffprobe said 157138, the real count was 157013 -- 125
        # frames high. Without the margin a capture near the end seeks past EOF
        # and ffmpeg simply produces no file, which surfaces as a mystifying
        # failure on the last screenshot of a run whose earlier frames worked.
        available = info.frame_count
        if not info.exact and available > 0:
            available -= max(_ESTIMATE_MARGIN_MIN, int(available * _ESTIMATE_MARGIN))
        self.frame_count = max(0, available - source.trim)

    # -- frame addressing ------------------------------------------------

    def _timestamp(self, frame: int) -> float:
        """Seconds to seek to for ``frame``.

        The half-frame offset targets the middle of the frame's display
        interval instead of its leading edge. Landing exactly on a boundary
        makes the choice between frame N and N-1 a floating point rounding
        question, and it does not always round the way you want.
        """
        absolute = frame + self._source.trim
        return (absolute + 0.5) / float(self.info.fps)

    # -- picture types ---------------------------------------------------

    @property
    def supports_frame_types(self) -> bool:
        """Whether picture types can actually be read from this file.

        Determined by trying, once, rather than assumed. ffprobe's
        ``-read_intervals`` returns nothing useful for some containers and
        broken timestamp tracks, and the failure is silent: every frame then
        looks like "not a B-frame", the selector rejects all of them, and the
        user gets "no usable frames were found" with no clue why. One probe in
        the middle of the clip turns that into an honest "this engine cannot
        tell picture types here".
        """
        if self._pict_types_usable is None:
            probe_frame = min(max(self.frame_count // 2, 0), max(self.frame_count - 1, 0))
            self._scan_pict_types(probe_frame)
            self._pict_types_usable = bool(self._pict_cache)
        return self._pict_types_usable

    @property
    def has_overlay(self) -> bool:
        """Always False.

        Burning the source name in would need ffmpeg's drawtext filter, which
        needs a font file path. There is no portable way to name one across
        Windows, Linux and macOS, and this is not theoretical: drawtext with no
        explicit font crashed outright -- an access violation, not an error
        message -- on the development machine. A comparison where the label
        silently failed to render on one machine is worse than one with no
        labels at all, so the orchestrator reports the difference instead.
        """
        return False

    def _scan_pict_types(self, frame: int, span_seconds: float = 4.0) -> None:
        """Read picture types for a window of frames around ``frame``.

        One ffprobe call covers a few dozen frames, so the nudging search in
        the selector does not pay a process launch per candidate.
        """
        for start, end in self._scanned:
            if start <= frame <= end:
                return

        begin = max(0.0, self._timestamp(frame) - 0.5)
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    str(self._ffprobe),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-read_intervals",
                    f"{begin:.6f}%+{span_seconds:.3f}",
                    "-show_entries",
                    "frame=pict_type,best_effort_timestamp_time",
                    "-of",
                    "csv=p=0",
                    str(self._source.path),
                ],  # fmt: skip
                capture_output=True,
                text=True,
                timeout=_FRAME_TIMEOUT,
                check=False,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return

        fps = float(self.info.fps)
        lowest, highest = frame, frame
        found = 0
        for line in proc.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 2:
                continue
            timestamp, pict_type = parts[0], parts[1]
            # ffprobe's field order follows the -show_entries order, but which
            # of the two is the timestamp depends on the build. Whichever one
            # parses as a float is the timestamp.
            try:
                seconds = float(timestamp)
            except ValueError:
                try:
                    seconds = float(pict_type)
                except ValueError:
                    continue
                pict_type = parts[0]
            absolute = int(round(seconds * fps))
            relative = absolute - self._source.trim
            self._pict_cache[relative] = pict_type
            found += 1
            lowest = min(lowest, relative)
            highest = max(highest, relative)

        # Only remember the window as scanned if something came back. Recording
        # an empty window would make every frame in it permanently "not a
        # B-frame" while looking exactly like a successful scan.
        if found:
            self._scanned.append((lowest, highest))

    def is_b_frame(self, frame: int) -> bool:
        if not 0 <= frame < self.frame_count:
            return False
        if not self.supports_frame_types:
            return False
        if frame not in self._pict_cache:
            self._scan_pict_types(frame)
        return self._pict_cache.get(frame) == "B"

    # -- luma ------------------------------------------------------------

    def mean_luma(self, frame: int) -> float:
        """Average luma of the active picture area, in [0, 1].

        Two shortcuts, both deliberate. The centre crop matches the VapourSynth
        engine: a scope film is about a quarter letterbox, and including the
        bars pulls the mean below the dark threshold on frames that are
        perfectly well lit. The 64x36 thumbnail is because the question is "is
        this essentially black", not what the histogram looks like, and
        decoding a full 4K frame to answer it costs far more than it is worth.
        """
        if not 0 <= frame < self.frame_count:
            return 0.0
        crop = f"crop=iw*{ACTIVE_AREA}:ih*{ACTIVE_AREA}"
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    str(self._ffmpeg),
                    "-v",
                    "error",
                    "-accurate_seek",
                    "-ss",
                    f"{self._timestamp(frame):.6f}",
                    "-i",
                    str(self._source.path),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"{crop},scale=64:36,format=gray",
                    "-f",
                    "rawvideo",
                    "-",
                ],  # fmt: skip
                capture_output=True,
                timeout=_FRAME_TIMEOUT,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return 0.0
        if proc.returncode != 0 or not proc.stdout:
            return 0.0
        return sum(proc.stdout) / (len(proc.stdout) * 255.0)

    # -- capture ---------------------------------------------------------

    def write_frames(self, frames: list[int], directory: Path) -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        for frame in frames:
            if not 0 <= frame < self.frame_count:
                raise EngineError(
                    f"{self.name}: frame {frame} is outside the clip (0..{self.frame_count - 1})"
                )
            path = directory / f"{frame:06d}.png"
            args = [
                str(self._ffmpeg), "-y", "-v", "error", "-accurate_seek",
                "-ss", f"{self._timestamp(frame):.6f}",
                "-i", str(self._source.path),
                "-frames:v", "1",
            ]  # fmt: skip
            if self._filters:
                args += ["-vf", ",".join(self._filters)]
            args += ["-pix_fmt", "rgb24", str(path)]

            try:
                proc = subprocess.run(  # noqa: S603
                    args,
                    capture_output=True,
                    text=True,
                    timeout=_FRAME_TIMEOUT,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                raise EngineError(f"{self.name}: timed out writing frame {frame}") from exc

            if proc.returncode != 0 or not path.is_file():
                detail = (proc.stderr or "").strip().splitlines()
                raise EngineError(
                    f"{self.name}: ffmpeg failed on frame {frame}: "
                    f"{detail[-1] if detail else 'no output produced'}"
                )
            written.append(path)
        return written

    def close(self) -> None:
        self._pict_cache.clear()


class FfmpegEngine:
    name = "ffmpeg"

    def available(self, tools: Mapping[str, str] | None = None) -> bool:
        tools = tools or {}
        return binaries.find_binary("ffmpeg", tools.get("ffmpeg")) is not None

    def _tonemap_mode(self, source: Source, info: VideoInfo) -> Tonemap:
        if source.tonemap is not Tonemap.AUTO:
            return source.tonemap
        return {
            HdrFormat.HDR10: Tonemap.HDR10,
            HdrFormat.HLG: Tonemap.HDR10,
            HdrFormat.DOVI: Tonemap.HDR10,
            HdrFormat.SDR: Tonemap.NONE,
        }[info.hdr_format]

    def prepare(
        self,
        source: Source,
        *,
        target_fps: Fraction | None = None,
        overlay: bool = True,
        tools: dict[str, str] | None = None,
        progress: Callable[[str], None] | None = None,
        index_dir: Path | None = None,
        render: RenderSettings | None = None,
    ) -> FfmpegSource:
        # index_dir is ignored: this engine seeks per frame and builds no index.
        tools = tools or {}
        if render is not None:
            raise EngineError(
                f"{source.name}: the ffmpeg engine cannot render one frame several different "
                f"ways. Tone-mapping curves and GLSL shaders only exist inside a player, so a "
                f"settings comparison is mpv's job."
            )
        if not source.path.is_file():
            raise EngineError(f"{source.name}: no such file: {source.path}")

        ffmpeg = binaries.require_binary("ffmpeg", tools.get("ffmpeg"))
        ffprobe = binaries.require_binary("ffprobe", tools.get("ffprobe"))
        info = probe(source.path, ffprobe=tools.get("ffprobe"))

        # Order matches config.PROCESSING_ORDER. Trim is not a filter here --
        # it is folded into the seek timestamp -- and fps normalisation only
        # affects how frame indices map to time, which _timestamp handles.
        filters: list[str] = []
        if source.resize:
            width, height = source.resize
            filters.append(f"scale={width}:{height}:flags=spline")
        if source.crop:
            left, right, top, bottom = source.crop
            filters.append(f"crop=in_w-{left + right}:in_h-{top + bottom}:{left}:{top}")

        mode = self._tonemap_mode(source, info)
        if mode is Tonemap.DOVI:
            raise EngineError(
                f"{source.name}: the ffmpeg engine cannot use Dolby Vision metadata. "
                f"Use the VapourSynth engine, or set tonemap = 'hdr10' to tonemap the "
                f"HDR10 base layer instead."
            )
        if mode is Tonemap.HDR10PLUS:
            raise EngineError(
                f"{source.name}: the ffmpeg engine cannot use HDR10+ metadata. "
                f"Use the VapourSynth engine, or set tonemap = 'hdr10'."
            )
        if mode is Tonemap.HDR10:
            filters.append(_TONEMAP_CHAIN)

        if source.luma_fix:
            raise EngineError(
                f"{source.name}: luma_fix is only implemented in the VapourSynth engine."
            )

        return FfmpegSource(source, info, ffmpeg, ffprobe, filters, target_fps)
