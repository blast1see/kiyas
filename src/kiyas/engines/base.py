"""The contract every frame engine implements.

The orchestrator works entirely through these two protocols, so swapping
VapourSynth for ffmpeg changes nothing above this line.

The split is deliberate: :class:`FrameEngine` is stateless and answers "can you
run here, and can you open this file". :class:`PreparedSource` is one clip with
every transformation already applied, which is the only object the rest of the
program is allowed to capture from. Nothing outside an engine may apply a crop
or a tonemap, because the *order* those happen in is part of the correctness of
the result and it is defined once, in :data:`kiyas.config.PROCESSING_ORDER`.
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import Source

#: Mean luma below which a frame is treated as too dark to be worth comparing.
#:
#: PlaneStats normalises to [0, 1] over the full integer range, so limited
#: range black (16/255) sits at 0.063. The threshold is a little above that so
#: fades and black leader are rejected while a genuinely dark but detailed
#: night scene -- the most interesting thing to compare, since that is where
#: banding and block noise show -- is kept.
DARK_LUMA_THRESHOLD = 0.10

#: Fraction of the frame brightness is measured over, centred.
#:
#: Not the whole frame. A 2.39:1 film in a 16:9 container is roughly a quarter
#: black bars, and including them drags the mean below DARK_LUMA_THRESHOLD on
#: shots that are perfectly well lit -- observed on a real scope remux, where
#: the dark-frame rule threw away usable frames for that reason alone. 0.6 is
#: wide enough to stay representative of the picture and narrow enough to sit
#: inside the bars of the widest common aspect ratio.
#:
#: Both engines must use the same value, or the same project selects different
#: frames depending on which engine ran.
ACTIVE_AREA = 0.6


class EngineError(RuntimeError):
    """Raised when an engine cannot do what was asked of it."""


@runtime_checkable
class PreparedSource(Protocol):
    """One source, fully transformed, ready to capture frames from."""

    name: str
    frame_count: int
    fps: Fraction

    @property
    def supports_frame_types(self) -> bool:
        """Whether :meth:`is_b_frame` returns real information.

        An engine that cannot tell picture types must say so rather than
        guessing, so the orchestrator can warn that B-frame selection is off
        instead of silently comparing I-frames.
        """
        ...

    @property
    def has_overlay(self) -> bool:
        """Whether the source name was burnt into the frame.

        Engines differ here, and the difference is visible in the output: a
        comparison where half the sources are labelled and half are not is
        worse than one where none of them are. The orchestrator checks this
        across all sources and says so rather than shipping a mixed set.
        """
        ...

    def is_b_frame(self, frame: int) -> bool: ...

    def mean_luma(self, frame: int) -> float:
        """Average luma in [0, 1]."""
        ...

    def write_frames(self, frames: list[int], directory: Path) -> list[Path]:
        """Write one PNG per frame into ``directory``; return the paths written."""
        ...

    def close(self) -> None: ...


@runtime_checkable
class FrameEngine(Protocol):
    name: str

    def available(self) -> bool:
        """Whether this engine can run in the current environment."""
        ...

    def prepare(
        self,
        source: Source,
        *,
        target_fps: Fraction | None = None,
        overlay: bool = True,
        tools: dict[str, str] | None = None,
        progress: Callable[[str], None] | None = None,
        index_dir: Path | None = None,
    ) -> PreparedSource:
        """Open ``source`` and apply its transformations in the fixed order.

        ``progress`` receives short status lines. Opening a source can take
        half a minute while an indexer walks a 50 GB file, and an engine that
        goes silent for that long looks hung.

        ``index_dir`` relocates indexer cache files. By default they are
        written next to the media, which is what makes a second run fast; point
        this elsewhere for a read-only or shared library. Engines that do not
        build an index ignore it.
        """
        ...
