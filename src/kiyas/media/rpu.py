"""Reading where the picture is, out of a Dolby Vision RPU.

A scope film in a 16:9 container is mostly black bar, and two releases of the
same title rarely agree about it: one ships the frame the master was graded in
and encodes the bars, the next ships the picture alone. Compared side by side
those produce columns of different shapes, which cannot be flipped between --
and flipping between them is the one thing a comparison is for.

The bars cannot be measured off the picture. ``cropdetect`` finds nothing on a
PQ source, because "black" there is not zero, and splitting the difference
arithmetically comes out a row or two wrong -- which is the worst kind of
wrong, since it looks like an answer. The file already carries the real one: a
Dolby Vision RPU's level 5 block states the active area the master was graded
for.

Reading it is nearly free. ``dovi_tool extract-rpu --limit`` stops after a
handful of frames and closes the pipe, so one sample takes about 0.2 seconds
against a 78 GB remux; the whole file is never read.

What this must not do is answer from one place in the film. Plenty of titles
change shape as they play -- an IMAX sequence opens out to 1.78:1 and closes
back to scope -- and a sample taken anywhere inside one of those stretches
looks perfectly constant. Measured elsewhere on real remuxes: The Dark Knight
carries 39,602 IMAX frames against 179,353 scope ones with the first change at
frame 1274, and Oppenheimer and Guardians of the Galaxy Vol. 3 both switch too.
So several positions spread across the film are read, and the distinct shapes
are reported rather than averaged.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import binaries

#: How many places in the film to read. Each costs about 0.2 seconds, so this
#: is cheap to raise; what it has to be is more than one, because a single
#: reading cannot tell a fixed shape from the inside of an IMAX sequence.
_POSITIONS = 5

#: Frames to let dovi_tool parse before it stops. One would do, since the level
#: 5 block is on every frame rather than some of them, but a seek lands on the
#: nearest keyframe and a little slack costs nothing at this speed.
_LIMIT = 48

#: Reading a few frames is instant. This is here to catch a pipe that has
#: stopped moving, not to bound honest work.
_SAMPLE_TIMEOUT = 120.0

#: How far from each end to stay. Opening logos and closing credits are often
#: full-frame on a film that is otherwise scope, and they are not what is being
#: compared -- sampling them would report a shape change that no viewer of the
#: film proper would ever see.
_MARGIN = 0.1

#: The four fields a level 5 block carries. Named here rather than read
#: loosely, because a block that is missing one of them is not a block that
#: says "no bars".
_OFFSETS = (
    "active_area_left_offset",
    "active_area_right_offset",
    "active_area_top_offset",
    "active_area_bottom_offset",
)


class ActiveAreaError(RuntimeError):
    """Raised when the RPU is there but cannot be read.

    Deliberately distinct from a reading that found no level 5 block. "There is
    no level 5 here" and "I could not understand this" are opposite answers,
    and collapsing them into one value is exactly how a parser comes to report
    a film whose shape changes as a film with no metadata at all.
    """


@dataclass(frozen=True)
class ActiveArea:
    """Where the picture sits, as padding around it."""

    left: int
    right: int
    top: int
    bottom: int

    @property
    def is_whole_frame(self) -> bool:
        """Whether the metadata says nothing is being masked off."""
        return not (self.left or self.right or self.top or self.bottom)

    def size_within(self, width: int, height: int) -> tuple[int, int]:
        """What is left of a ``width`` x ``height`` frame once the bars go."""
        return width - self.left - self.right, height - self.top - self.bottom

    def as_crop(self) -> tuple[int, int, int, int]:
        """In the order a project file's ``crop`` is written: left, right, top, bottom."""
        return (self.left, self.right, self.top, self.bottom)


@dataclass(frozen=True)
class Reading:
    """What the RPU said about the picture, across every position read."""

    #: The distinct shapes found, in the order they were first seen.
    shapes: tuple[ActiveArea, ...]

    #: How many positions were read.
    positions: int

    #: How many of them carried a level 5 block at all.
    carrying: int

    @property
    def absent(self) -> bool:
        """Whether the file carries no level 5 anywhere that was looked.

        A release cropped to its own picture has no bars to mask and therefore
        no reason to carry the block; measured on a real iTunes WEB-DL, which
        has none at any position.
        """
        return self.carrying == 0

    @property
    def fixed(self) -> ActiveArea | None:
        """The one shape, when every position agreed on it."""
        if self.carrying == self.positions and len(self.shapes) == 1:
            return self.shapes[0]
        return None

    @property
    def varies(self) -> bool:
        """Whether the picture changes shape as the film plays.

        Positions that carry no level 5 while others do count as a change, not
        as missing data: no block means no masking, which is itself a shape.
        """
        return len(self.shapes) > 1 or 0 < self.carrying < self.positions


def positions_across(duration: float, positions: int = _POSITIONS) -> list[float]:
    """Evenly spaced seconds to read, kept away from both ends."""
    if duration <= 0:
        return [0.0]
    start = duration * _MARGIN
    span = duration * (1 - 2 * _MARGIN)
    if positions <= 1:
        return [start + span / 2]
    return [start + span * index / (positions - 1) for index in range(positions)]


def read_active_area(payload: dict) -> ActiveArea | None:
    """The active area in one parsed RPU frame, or ``None`` if it carries no level 5.

    Kept separate from the subprocess work so the parsing can be pinned down
    against captured payloads without a media file.
    """
    for block in _extension_blocks(payload):
        if not isinstance(block, dict):
            continue
        level5 = block.get("Level5")
        if level5 is None:
            continue
        if not isinstance(level5, dict) or any(field not in level5 for field in _OFFSETS):
            raise ActiveAreaError(
                "the RPU has a level 5 block without the four active area offsets: "
                f"{sorted(level5) if isinstance(level5, dict) else level5!r}"
            )
        try:
            return ActiveArea(*(int(level5[field]) for field in _OFFSETS))
        except (TypeError, ValueError) as exc:
            raise ActiveAreaError(
                f"the RPU's level 5 offsets are not whole numbers: "
                f"{[level5[field] for field in _OFFSETS]!r}"
            ) from exc
    return None


def sample(
    source: Path,
    *,
    duration: float,
    ffmpeg: str | Path | None = None,
    dovi_tool: str | Path | None = None,
    positions: int = _POSITIONS,
) -> Reading:
    """Read ``source``'s active area at several places along it.

    Raises :class:`ActiveAreaError` when the RPU cannot be read, and
    :class:`~kiyas.media.binaries.BinaryNotFound` when either tool is missing.
    A caller that only wants to enrich a message should catch both: an
    unreadable RPU is a reason to say less, not a reason to fail a run that has
    already produced its images.
    """
    ffmpeg_path = binaries.require_binary("ffmpeg", ffmpeg)
    dovi_path = binaries.require_binary("dovi_tool", dovi_tool)

    shapes: list[ActiveArea] = []
    carrying = 0
    seconds = positions_across(duration, positions)
    for at in seconds:
        area = _read_one(source, at, ffmpeg_path, dovi_path)
        if area is None:
            continue
        carrying += 1
        if area not in shapes:
            shapes.append(area)
    return Reading(shapes=tuple(shapes), positions=len(seconds), carrying=carrying)


def _extension_blocks(payload: dict) -> list:
    """Every extension-metadata block in a parsed RPU, wherever it is kept.

    dovi_tool nests these under the content-mapping version the RPU uses, and
    which one that is depends on the master. Looking in both the nested and the
    flat position costs nothing; looking in only one and finding nothing would
    report a file that has a level 5 block as having none -- the silence this
    module exists to avoid.
    """
    metadata = payload.get("vdr_dm_data")
    if not isinstance(metadata, dict):
        return []
    blocks: list = []
    for key, value in metadata.items():
        if key == "ext_metadata_blocks" and isinstance(value, list):
            blocks.extend(value)
        elif isinstance(value, dict) and isinstance(value.get("ext_metadata_blocks"), list):
            blocks.extend(value["ext_metadata_blocks"])
    return blocks


def _read_one(
    source: Path, seconds: float, ffmpeg_path: Path, dovi_path: Path
) -> ActiveArea | None:
    """Extract a few frames of RPU at ``seconds`` and read the level 5 out of the first."""
    with tempfile.TemporaryDirectory(prefix="kiyas-rpu-") as workspace:
        rpu = Path(workspace) / "sample.rpu"
        # -ss ahead of -i so ffmpeg seeks instead of decoding its way there.
        # With -c copy that lands on the nearest keyframe, which is all this
        # needs: the level 5 block is on every frame, not just some of them.
        copy = [
            str(ffmpeg_path), "-v", "error", "-nostdin", "-ss", f"{seconds:.3f}",
            "-i", str(source), "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-",
        ]  # fmt: skip
        extract = [
            str(dovi_path), "extract-rpu", "--limit", str(_LIMIT), "-o", str(rpu), "-",
        ]  # fmt: skip

        try:
            with subprocess.Popen(  # noqa: S603
                copy,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=binaries.no_window_flag(),
            ) as reader:
                assert reader.stdout is not None
                try:
                    result = subprocess.run(  # noqa: S603
                        extract,
                        stdin=reader.stdout,
                        capture_output=True,
                        text=True,
                        timeout=_SAMPLE_TIMEOUT,
                        check=False,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=binaries.no_window_flag(),
                    )
                finally:
                    # Closing this end first stops ffmpeg filling a pipe nobody
                    # is reading, which is the ordinary case here: --limit
                    # means dovi_tool leaves long before the file is done.
                    reader.stdout.close()
                    reader.terminate()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActiveAreaError(f"could not read the RPU of {source.name}: {exc}") from exc

        if not rpu.is_file() or rpu.stat().st_size == 0:
            said = (result.stderr or "").strip().splitlines()
            raise ActiveAreaError(
                f"no Dolby Vision RPU came out of {source.name} at {seconds:.0f}s"
                + (f": {said[-1]}" if said else "")
            )

        return _parse_first_frame(rpu, dovi_path, source)


def _parse_first_frame(rpu: Path, dovi_path: Path, source: Path) -> ActiveArea | None:
    """Ask dovi_tool for the first frame of ``rpu`` as JSON and read its level 5."""
    try:
        shown = subprocess.run(  # noqa: S603
            [str(dovi_path), "info", "-i", str(rpu), "-f", "0"],
            capture_output=True,
            text=True,
            timeout=_SAMPLE_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
            creationflags=binaries.no_window_flag(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActiveAreaError(f"could not parse the RPU of {source.name}: {exc}") from exc

    text = shown.stdout or ""
    if shown.returncode != 0:
        said = (shown.stderr or text).strip().splitlines()
        raise ActiveAreaError(
            f"dovi_tool could not describe the RPU of {source.name}"
            + (f": {said[-1]}" if said else "")
        )
    return read_active_area(payload_from(text, source.name))


def payload_from(text: str, what: str) -> dict:
    """The JSON document inside dovi_tool's output.

    ``dovi_tool info`` prints "Parsing RPU file..." on standard output before
    the document, so its output is not JSON from the first character and
    ``json.loads`` on the whole of it fails against every file there is. This
    is the sort of thing that works on the machine it was written on and stops
    the day the tool adds a line, so it has a test of its own.
    """
    start = text.find("{")
    if start < 0:
        said = text.strip().splitlines()
        raise ActiveAreaError(
            f"dovi_tool printed no RPU document for {what}" + (f": {said[-1]}" if said else "")
        )
    # No isinstance check on the way out: the slice starts at a brace, so
    # json.loads either raises or hands back an object.
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise ActiveAreaError(f"the RPU of {what} did not parse as JSON: {exc}") from exc
