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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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

#: Size the picture is reduced to before brightness is measured, and the reason
#: it is shared rather than chosen per engine.
#:
#: The question is "is this essentially black", not what the histogram looks
#: like, so a thumbnail answers it and decoding a full 4K frame to find out
#: does not. Shared because measuring the *same* reduction is what makes the
#: engines agree: they must also reduce to full-range grey, or a limited-range
#: plane puts black at 16/255 and the same threshold means two different
#: things. Measured on a real remux before this was shared: opposite verdicts
#: on two frames out of six.
LUMA_SAMPLE = (64, 36)


class EngineError(RuntimeError):
    """Raised when an engine cannot do what was asked of it."""


@dataclass(frozen=True, slots=True)
class RenderSettings:
    """One column of a settings comparison: a name and how to render it.

    ``options`` are renderer-specific -- today that means mpv options, because
    mpv is the only engine that can render the same frame two different ways.
    An engine handed options it cannot honour must refuse rather than ignore
    them: a settings comparison whose variants were all rendered identically
    looks like a finding ("no difference at all") instead of a failure.

    ``width`` and ``fullscreen`` describe the capture size and belong here
    rather than on the source, because they are a property of the comparison:
    every column has to be the same size or the pictures cannot be flipped
    between.
    """

    name: str = ""
    options: Mapping[str, str] = field(default_factory=dict)
    width: int | None = None
    fullscreen: bool = False


@runtime_checkable
class PreparedSource(Protocol):
    """One source, fully transformed, ready to capture frames from."""

    name: str
    frame_count: int
    fps: Fraction

    @property
    def supports_frame_types(self) -> bool:
        """Whether :meth:`picture_type` returns real information.

        An engine that cannot tell picture types must say so rather than
        guessing, so the orchestrator can warn that B-frame selection is off
        instead of silently comparing I-frames.
        """
        ...

    @property
    def has_b_frames(self) -> bool:
        """Whether this encode contains B-frames at all.

        Not every one does: a WEB-DL encoded for low latency can be entirely
        I and P frames, and on one the B-frame rule rejects every frame in the
        file. The orchestrator asks first so it can fall back to "not an
        I-frame" -- which is what the rule was ever about -- rather than
        refusing to produce a comparison.
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

    def picture_type(self, frame: int) -> str | None:
        """``I``, ``P``, ``B``, or ``None`` when it cannot be read."""
        ...

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

    def available(self, tools: Mapping[str, str] | None = None) -> bool:
        """Whether this engine can run in the current environment.

        ``tools`` is the project's ``[tools]`` table, so an executable the user
        pointed at explicitly counts as present even when it is not on PATH.
        """
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
        render: RenderSettings | None = None,
    ) -> PreparedSource:
        """Open ``source`` and apply its transformations in the fixed order.

        ``progress`` receives short status lines. Opening a source can take
        half a minute while an indexer walks a 50 GB file, and an engine that
        goes silent for that long looks hung.

        ``index_dir`` relocates indexer cache files. By default they are
        written next to the media, which is what makes a second run fast; point
        this elsewhere for a read-only or shared library. Engines that do not
        build an index ignore it.

        ``render`` is set only for a settings comparison. An engine that cannot
        vary its rendering must raise :class:`EngineError` rather than ignore
        it.
        """
        ...
