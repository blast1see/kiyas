"""The mpv engine: the only one that can answer "how should I configure this".

For a source comparison this is the third choice, behind VapourSynth and
ffmpeg, and `doctor` says so. For a settings comparison it is the only choice
there is: tone-mapping curves and GLSL shaders exist inside a player's
renderer and nowhere else, so no amount of VapourSynth will tell you whether
``st2094-40`` looks better than ``bt.2446a`` on your display.

**What it captures.** ``screenshot window``, which is mpv's rendered output --
shader, tone curve, scaler, dither, everything. That is the whole point. It
also means the capture is the size of mpv's window, and a window cannot be
bigger than the screen: on a 2560x1440 display a 4K source captures at most
2474x1392 windowed, or exactly 2560x1440 with ``fullscreen = true``. The size
that came out is recorded, because a comparison silently produced at a
different resolution than the one before it is not comparable with it.

**Where the frame numbers come from.** Not from mpv. Picture types and
brightness are measured with ffprobe, through the same code the ffmpeg engine
uses, for two reasons. One ffprobe call reads the picture type of a few dozen
frames, while mpv can only report the frame it is currently showing -- so
finding a B-frame near a target position costs one process launch here and one
seek *per candidate* there. And using one implementation across engines is what
stops the same project file from selecting different frames depending on which
engine happened to run.

**mpv is started only when frames are about to be written.** Preparing every
source up front would leave one mpv window per column open at once, all of them
covering each other and competing for the screen. Capture is sequential
anyway, so nothing is lost by opening the player at the last moment.
"""

from __future__ import annotations

import itertools
import tempfile
from collections.abc import Callable, Mapping
from fractions import Fraction
from pathlib import Path

from ..config import Source, Tonemap
from ..media import binaries
from ..media.probe import HdrFormat, probe
from ..mpvctl.session import MpvSession, SessionError
from .base import EngineError, RenderSettings
from .ffmpeg import FfmpegEngine, FfmpegSource

_TAGS = itertools.count(1)


class MpvSource:
    """One source (or one variant of one source), rendered by mpv."""

    def __init__(
        self,
        source: Source,
        measure: FfmpegSource,
        *,
        mpv: Path,
        display_name: str,
        source_fps: Fraction,
        target_fps: Fraction | None,
        overlay: bool,
        options: dict[str, str],
        width: int | None,
        fullscreen: bool,
        assumptions: list[str],
    ):
        self.name = display_name
        self.assumptions = assumptions
        self._measure = measure
        self._source = source
        self._mpv = mpv
        self._source_fps = source_fps
        self._overlay = overlay
        self._options = options
        self._width = width
        self._fullscreen = fullscreen
        self._session: MpvSession | None = None

        self.fps = target_fps or source_fps
        self.frame_count = measure.frame_count

    # -- measurement, delegated ------------------------------------------

    @property
    def supports_frame_types(self) -> bool:
        return self._measure.supports_frame_types

    @property
    def has_overlay(self) -> bool:
        return self._overlay

    @property
    def has_b_frames(self) -> bool:
        return self._measure.has_b_frames

    def picture_type(self, frame: int) -> str | None:
        return self._measure.picture_type(frame)

    def mean_luma(self, frame: int) -> float:
        return self._measure.mean_luma(frame)

    # -- capture ---------------------------------------------------------

    def _start(self) -> MpvSession:
        if self._session is not None:
            return self._session
        config_dir = Path(tempfile.gettempdir()) / "kiyas-mpv-profile"
        try:
            session = MpvSession(
                self._mpv,
                config_dir=config_dir,
                tag=f"{_tag(self.name)}-{next(_TAGS)}",
                options=self._options,
                width=self._width,
                fullscreen=self._fullscreen,
                label=self.name if self._overlay else None,
            )
        except SessionError as exc:
            raise EngineError(f"{self.name}: {exc}") from exc
        try:
            session.load(self._source.path)
        except SessionError as exc:
            session.close()
            raise EngineError(f"{self.name}: {exc}") from exc
        self._session = session
        return session

    def write_frames(self, frames: list[int], directory: Path) -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        session = self._start()
        written: list[Path] = []
        for frame in frames:
            if not 0 <= frame < self.frame_count:
                raise EngineError(
                    f"{self.name}: frame {frame} is outside the clip (0..{self.frame_count - 1})"
                )
            # Trim shifts the index space exactly as it does in the other
            # engines: frame 0 here is frame `trim` in the file. The caption
            # goes to `capture` rather than being set here, because when it is
            # applied decides whether the picture is the right one -- see
            # MpvSession._caption.
            try:
                session.capture(
                    frame + self._source.trim,
                    self._source_fps,
                    directory / f"{frame:06d}.png",
                    label=f"{self.name}   frame {frame}" if self._overlay else None,
                )
            except SessionError as exc:
                raise EngineError(f"{self.name}: {exc}") from exc
            written.append(directory / f"{frame:06d}.png")

        size = session.capture_size
        if size:
            note = f"rendered by mpv at {size[0]}x{size[1]}"
            if self._width and size[0] != self._width:
                note += (
                    f", not the {self._width} px asked for -- a window cannot be larger than "
                    f"the display. Use fullscreen = true, or a smaller width."
                )
            self.assumptions.append(note)
        if session.best_effort:
            self.assumptions.append(
                "mpv could not report what its renderer had drawn (no vo-passes), so captures "
                "were timed rather than confirmed. Check that the frames look like the frames "
                "you asked for."
            )
        return written

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        self._measure.close()


class MpvEngine:
    name = "mpv"

    def available(self, tools: Mapping[str, str] | None = None) -> bool:
        # ffprobe is not optional: it is where frame counts, picture types and
        # brightness come from.
        tools = tools or {}
        return all(
            binaries.find_binary(name, tools.get(name)) is not None
            for name in ("mpv", "ffmpeg", "ffprobe")
        )

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
    ) -> MpvSource:
        # index_dir is ignored: mpv seeks, it does not build an index.
        tools = tools or {}
        if not source.path.is_file():
            raise EngineError(f"{source.name}: no such file: {source.path}")

        mpv = binaries.require_binary("mpv", tools.get("mpv"))
        info = probe(source.path, ffprobe=tools.get("ffprobe"))
        _refuse_unsupported(source)

        assumptions: list[str] = []
        if info.hdr_format is not HdrFormat.SDR:
            assumptions.append(
                f"{info.hdr_format.value} source tonemapped by mpv's renderer, not by kiyas"
            )

        options: dict[str, str] = dict(render.options) if render else {}
        if source.crop:
            left, right, top, bottom = source.crop
            width = info.width - left - right
            height = info.height - top - bottom
            if width <= 0 or height <= 0:
                raise EngineError(
                    f"{source.name}: crop {source.crop} leaves nothing of a "
                    f"{info.width}x{info.height} picture"
                )
            options.setdefault("video-crop", f"{width}x{height}+{left}+{top}")

        display_name = render.name if render and render.name else source.name
        if progress:
            progress(f"measuring {display_name}")

        measure = FfmpegEngine().prepare(
            source, target_fps=target_fps, overlay=False, tools=tools, progress=None
        )
        return MpvSource(
            source,
            measure,
            mpv=mpv,
            display_name=display_name,
            source_fps=info.fps,
            target_fps=target_fps,
            overlay=overlay,
            options=options,
            # Default to the source's own width: it is the only capture size
            # that adds no scaling of its own. mpv clamps it to the display.
            width=(render.width if render else None) or info.width,
            fullscreen=bool(render and render.fullscreen),
            assumptions=assumptions,
        )


def _refuse_unsupported(source: Source) -> None:
    """Reject per-source settings this engine cannot honour.

    Refusing rather than ignoring, in every case. A resize that quietly did
    nothing would produce a comparison at the wrong resolution; a tonemap
    setting that quietly did nothing would produce one whose columns were all
    rendered the same way.
    """
    if source.resize:
        raise EngineError(
            f"{source.name}: the mpv engine cannot resize a source. mpv renders into a window "
            f"and the window size is the output size, so set the capture width in [settings] "
            f"instead -- it applies to every column, which is what a comparison needs."
        )
    if source.luma_fix:
        raise EngineError(f"{source.name}: luma_fix is only implemented in the VapourSynth engine.")
    if source.tonemap is not Tonemap.AUTO:
        raise EngineError(
            f"{source.name}: the mpv engine does not take a 'tonemap' setting. mpv's renderer "
            f"decides how to map HDR to the SDR image it writes, and *that* is the thing a "
            f"settings comparison varies -- use mode = 'settings' with template = 'tonemap' "
            f"to compare curves."
        )


def _tag(name: str) -> str:
    """A pipe-name-safe tag. Named pipes reject most punctuation."""
    cleaned = "".join(character if character.isalnum() else "-" for character in name)
    return cleaned.strip("-")[:40] or "session"
