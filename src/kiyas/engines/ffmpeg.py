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
from functools import lru_cache
from pathlib import Path

from .. import assets
from ..config import Source, Tonemap
from ..media import binaries
from ..media.probe import HdrFormat, VideoInfo, probe
from .base import ACTIVE_AREA, LUMA_SAMPLE, EngineError, RenderSettings, label_for

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


#: Label height as a share of the frame height, with a floor in pixels.
#:
#: Not a fixed point size, for the same reason no threshold in this project is
#: fixed: the test clips here are 320x180 and the material is 3840x2160, an
#: order of magnitude apart, and a size legible on one is either unreadable or
#: a quarter of the picture on the other.
_LABEL_SHARE = 0.022
_LABEL_MIN_SIZE = 11
_LABEL_MARGIN_SHARE = 0.012
_LABEL_MARGIN_MIN = 6


@lru_cache(maxsize=4)
def _has_drawtext(ffmpeg: Path) -> bool:
    """Whether this ffmpeg build has the drawtext filter compiled in.

    It needs libfreetype at build time, and the minimal builds some package
    managers ship do not have it -- the same class of gap as the essentials
    build having no zscale, which CI already has to work around. Asked once per
    binary because the answer cannot change while the process runs, and the
    alternative is one extra launch per source.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [str(ffmpeg), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=_FRAME_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return " drawtext " in (proc.stdout or "")


def _escape_filter_path(path: Path) -> str:
    """A filesystem path as a filter option value.

    A Windows drive letter is the problem: ``C:/Users/...`` puts a colon where
    the filtergraph expects the next option to start.

    The colon needs **two** backslashes, not one. Measured against ffmpeg
    2026-08 with the argument passed directly to the process, no shell
    involved: ``C\\:/path`` is rejected with "No option name near
    '/Users/...'", because one backslash is consumed by the option-value parser
    and the colon then reaches the filtergraph splitter unprotected.
    ``C\\\\:/path`` survives both. Backslashes become forward slashes first, so
    the only backslashes left are the ones put there on purpose.
    """
    return str(path).replace("\\", "/").replace(":", "\\\\:")


class FfmpegSource:
    def __init__(
        self,
        source: Source,
        info: VideoInfo,
        ffmpeg: Path,
        ffprobe: Path,
        filters: list[str],
        target_fps: Fraction | None,
        label: str | None = None,
        font: Path | None = None,
    ):
        self.name = source.name
        self.info = info
        self.assumptions: list[str] = []
        self._source = source
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._filters = filters
        self._label = label
        self._font = font
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
        """Whether the label was actually burnt in.

        Not a constant. The label is drawn with ffmpeg's drawtext, which needs
        a font file by path -- there is no portable one, and calling it without
        an explicit font crashed outright, with an access violation rather than
        an error message, on the development machine. kiyas ships a font for
        that reason, so this is normally true; it is false when that font is
        missing from the install, or when the ffmpeg build has no drawtext
        filter compiled in. Both cases produce an unlabelled comparison, which
        the orchestrator reports rather than shipping a mixed set.
        """
        return self._label is not None and self._font is not None

    def _label_lines(self, frame: int) -> str:
        """The three things the label says, in the order FrameInfo says them.

        Same information as the VapourSynth engine's overlay so a comparison
        reads the same whichever engine produced it. The totals will not always
        agree: VapourSynth knows the exact frame count and this engine has
        duration times frame rate minus a safety margin, measured 125 frames
        high on a retail remux. That difference is a property of the engines
        and the label is simply the first place it becomes visible.
        """
        pict = self.picture_type(frame) if self.supports_frame_types else None
        return "\n".join(
            [
                f"Frame {frame} of {self.frame_count}",
                f"Picture type: {pict or 'N/A'}",
                "",
                "",
                self._label or "",
            ]
        )

    def _drawtext(self, textfile: Path) -> str:
        """The drawtext filter reading its text from ``textfile``.

        The text goes through a file rather than the ``text=`` option because
        the option cannot be escaped reliably. Three parsers read it in turn --
        the filtergraph splitter, the option-value parser and drawtext's own
        expansion -- and they do not agree with each other. Measured against
        ffmpeg 2026-08, one character at a time, with the argument passed
        straight to the process and no shell involved:

            ':' needs two backslashes, ',[];' need one, and an apostrophe
            cannot be made to work at all once any escaped colon follows it --
            "Director's Cut" plus "Picture type: B" in the same label is
            rejected however either one is escaped.

        Since a source name is free text -- "Director's Cut", "100% grade",
        "hibrit (DV P7 FEL + HDR10+)" -- a scheme with a known unfixable case
        is not a scheme. A file has no such rules, and ``textfile`` is what the
        filter offers for exactly this. Only its path needs escaping.

        ``expansion=none`` because kiyas writes the whole string itself: with
        expansion on, a '%' in a release name starts a directive.
        """
        height = self.info.height or 1080
        size = max(_LABEL_MIN_SIZE, int(height * _LABEL_SHARE))
        margin = max(_LABEL_MARGIN_MIN, int(height * _LABEL_MARGIN_SHARE))
        options = [
            f"fontfile={_escape_filter_path(self._font)}",
            f"textfile={_escape_filter_path(textfile)}",
            f"fontsize={size}",
            "fontcolor=white",
            "borderw=1",
            "bordercolor=black",
            f"x={margin}",
            f"y={margin}",
            "expansion=none",
        ]
        return "drawtext=" + ":".join(options)

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

    @property
    def has_b_frames(self) -> bool:
        """Whether the probe found any B-frame at all.

        Answered from the window scanned in the middle of the clip, which is
        where an encode's normal frame pattern lives. A file with B-frames has
        them everywhere; one without has none anywhere.
        """
        if not self.supports_frame_types:
            return False
        return "B" in self._pict_cache.values()

    def picture_type(self, frame: int) -> str | None:
        if not 0 <= frame < self.frame_count:
            return None
        if not self.supports_frame_types:
            return None
        if frame not in self._pict_cache:
            self._scan_pict_types(frame)
        return self._pict_cache.get(frame)

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
                    f"{crop},scale={LUMA_SAMPLE[0]}:{LUMA_SAMPLE[1]},format=gray",
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

    def luma_thumbnails(self, start: int, count: int, step: int = 1) -> list[bytes]:
        """``count`` consecutive luma thumbnails from ``start``.

        The same reduction :meth:`mean_luma` measures over -- same crop, same
        size, same full-range grey -- because two reductions that drift apart
        are two engines disagreeing about what they are looking at.

        One process for the whole run rather than one per frame. A search wide
        enough to be worth doing is thousands of frames, and at one launch each
        that is not a measurement anybody would wait for; decoded forward from
        a single seek it is one launch and a sequential read. A short return
        means the clip ended, and the caller sees fewer candidates rather than
        padding that would correlate with nothing.
        """
        if count <= 0 or self.frame_count <= 0:
            return []
        start = max(0, min(start, self.frame_count - 1))
        count = min(count, self.frame_count - start)
        crop = f"crop=iw*{ACTIVE_AREA}:ih*{ACTIVE_AREA}"
        chain = f"{crop},scale={LUMA_SAMPLE[0]}:{LUMA_SAMPLE[1]},format=gray"
        wanted = count
        if step > 1:
            # The decode is sequential either way; what a stride saves is the
            # scaling, the pipe and the ranking, which is most of the cost.
            chain = f"select=not(mod(n\\,{step})),{chain}"
            wanted = (count + step - 1) // step
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    str(self._ffmpeg),
                    "-v",
                    "error",
                    "-accurate_seek",
                    "-ss",
                    f"{self._timestamp(start):.6f}",
                    "-i",
                    str(self._source.path),
                    "-frames:v",
                    str(wanted),
                    "-vf",
                    chain,
                    "-fps_mode",
                    "passthrough",
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
            return []
        if proc.returncode != 0 or not proc.stdout:
            return []

        size = LUMA_SAMPLE[0] * LUMA_SAMPLE[1]
        data = proc.stdout
        return [data[i : i + size] for i in range(0, len(data) - size + 1, size)]

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
            # The label goes on last, after the tonemap chain, matching where
            # FrameInfo sits in the VapourSynth chain. Drawn before a tonemap
            # it would be tone mapped along with the picture.
            filters = list(self._filters)
            label_file = None
            if self.has_overlay:
                label_file = directory / f".kiyas-label-{frame:06d}.txt"
                label_file.write_text(self._label_lines(frame), encoding="utf-8")
                filters.append(self._drawtext(label_file))
            if filters:
                args += ["-vf", ",".join(filters)]
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

            if label_file is not None:
                label_file.unlink(missing_ok=True)

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
            # Careful what this promises. The VapourSynth engine applies the
            # ST2094-40 curve, which this chain has no equivalent for -- but it
            # does not apply HDR10+ per-scene metadata either, measured against
            # vs-placebo 2.0.4. Sending someone there for the metadata would be
            # sending them nowhere.
            raise EngineError(
                f"{source.name}: the ffmpeg engine has no ST2094-40 curve. "
                f"Use the VapourSynth engine, or set tonemap = 'hdr10'. Neither "
                f"engine applies HDR10+ per-scene metadata; both tone map the "
                f"HDR10 base statically."
            )
        if mode is Tonemap.HDR10:
            filters.append(_TONEMAP_CHAIN)

        if source.luma_fix:
            raise EngineError(
                f"{source.name}: luma_fix is only implemented in the VapourSynth engine."
            )

        label = font = None
        if overlay:
            font = assets.label_font()
            if font is None:
                prepared_notes = (
                    "no label was burnt in: the bundled font is missing from this install"
                )
            elif not _has_drawtext(ffmpeg):
                font = None
                prepared_notes = (
                    "no label was burnt in: this ffmpeg build has no drawtext filter, "
                    "which needs libfreetype"
                )
            else:
                prepared_notes = None
                label = label_for(source, mode)
        else:
            prepared_notes = None

        prepared = FfmpegSource(source, info, ffmpeg, ffprobe, filters, target_fps, label, font)
        if prepared_notes:
            prepared.assumptions.append(prepared_notes)
        return prepared
