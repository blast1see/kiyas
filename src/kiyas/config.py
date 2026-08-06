"""The project file.

A comparison is described by a TOML file rather than command-line flags. Six
sources with individual crops, trims and tonemapping do not fit on a command
line, and the description is worth keeping: it is what you edit when the sync
turns out to be one frame off, and what you hand to someone else so they can
reproduce the comparison exactly.

Validation is hand-written rather than delegated to a schema library. The
errors are the user interface here -- "source #2 ('WEB-DL'): crop needs 4
integers [left, right, top, bottom], got 2" is worth more than a dependency,
and a generic validator would have to be taught to say that anyway.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a project file cannot be understood."""


class Mode(Enum):
    """Which comparison axis this project describes."""

    SOURCE = "source"
    SETTINGS = "settings"


class Tonemap(Enum):
    AUTO = "auto"
    HDR10 = "hdr10"
    HDR10PLUS = "hdr10plus"
    DOVI = "dovi"
    NONE = "none"


class FrameMethod(Enum):
    INTERVAL = "interval"
    COUNT = "count"
    MANUAL = "manual"


class Engine(Enum):
    AUTO = "auto"
    VAPOURSYNTH = "vapoursynth"
    FFMPEG = "ffmpeg"
    MPV = "mpv"


# --------------------------------------------------------------------------
# Parsing helpers
#
# Every one of these takes the location as a string so the message can name the
# exact table the user has to go and edit.
# --------------------------------------------------------------------------


def _unknown_keys(table: dict[str, Any], known: set[str], where: str) -> None:
    """Reject keys we do not understand.

    Silently ignoring a typo is the worst outcome: `b_frame_only` instead of
    `b_frames_only` would leave the user believing a rule is active when it is
    not, and the screenshots would look plausible either way.
    """
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(k) for k in unknown)}. Known keys: {', '.join(sorted(known))}"
        )


def _enum(value: Any, enum: type[Enum], where: str, key: str) -> Any:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: '{key}' must be a string, got {type(value).__name__}")
    try:
        return enum(value.strip().lower())
    except ValueError:
        allowed = ", ".join(repr(m.value) for m in enum)
        raise ConfigError(f"{where}: '{key}' must be one of {allowed}, got {value!r}") from None


def _int(value: Any, where: str, key: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: '{key}' must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: '{key}' must be >= {minimum}, got {value}")
    return value


def _bool(value: Any, where: str, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be true or false, got {value!r}")
    return value


def _int_tuple(value: Any, length: int, where: str, key: str, labels: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ConfigError(f"{where}: '{key}' needs {length} integers {labels}, got {value!r}")
    out = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"{where}: '{key}' entry {index} must be an integer, got {item!r}")
        if item < 0:
            raise ConfigError(f"{where}: '{key}' entry {index} must not be negative, got {item}")
        out.append(item)
    return tuple(out)


def _fraction(value: Any, where: str, key: str) -> float:
    """Accept ``0.05``, ``"5%"`` or ``5`` and return a fraction of the runtime.

    A bare number above 1 is read as a percentage because writing ``skip_start
    = 5`` and meaning "five percent" is the obvious mistake, and reading it as
    "500%" would silently produce an empty comparison.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text.endswith("%"):
            raise ConfigError(f"{where}: '{key}' as a string must end with '%', got {value!r}")
        try:
            number = float(text[:-1])
        except ValueError:
            raise ConfigError(f"{where}: '{key}' is not a number: {value!r}") from None
        fraction = number / 100.0
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: '{key}' must be a number or a percentage string")
    else:
        fraction = float(value)
        if fraction > 1.0:
            fraction /= 100.0

    if not 0.0 <= fraction < 0.5:
        raise ConfigError(
            f"{where}: '{key}' must be between 0% and 50%, got {value!r}. "
            f"Skipping half the runtime from one end leaves nothing to compare."
        )
    return fraction


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Source:
    """One clip in a comparison, plus everything done to it before capture.

    The processing order is fixed and is not the order these fields are
    declared in -- see :data:`PROCESSING_ORDER`.
    """

    path: Path
    name: str
    trim: int = 0
    crop: tuple[int, int, int, int] | None = None
    resize: tuple[int, int] | None = None
    tonemap: Tonemap = Tonemap.AUTO
    luma_fix: bool = False
    normalize_fps: bool = True

    _KEYS = {
        "path",
        "name",
        "trim",
        "crop",
        "resize",
        "tonemap",
        "luma_fix",
        "normalize_fps",
    }

    @classmethod
    def parse(cls, table: dict[str, Any], index: int) -> Source:
        where = f"source #{index}"
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: each [[source]] must be a table")

        raw_name = table.get("name")
        if raw_name is not None and isinstance(raw_name, str) and raw_name.strip():
            where = f"source #{index} ({raw_name.strip()!r})"

        _unknown_keys(table, cls._KEYS, where)

        raw_path = table.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ConfigError(f"{where}: 'path' is required and must be a non-empty string")

        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError(
                f"{where}: 'name' is required. It is burned into the frame and becomes "
                f"the column label in the published comparison, so it cannot be guessed."
            )

        source = cls(path=Path(raw_path.strip()).expanduser(), name=raw_name.strip())

        if "trim" in table:
            source.trim = _int(table["trim"], where, "trim")
        if "crop" in table:
            source.crop = _int_tuple(  # type: ignore[assignment]
                table["crop"], 4, where, "crop", "[left, right, top, bottom]"
            )
        if "resize" in table:
            resize = _int_tuple(table["resize"], 2, where, "resize", "[width, height]")
            if resize[0] == 0 or resize[1] == 0:
                raise ConfigError(f"{where}: 'resize' dimensions must be greater than zero")
            source.resize = resize  # type: ignore[assignment]
        if "tonemap" in table:
            source.tonemap = _enum(table["tonemap"], Tonemap, where, "tonemap")
        if "luma_fix" in table:
            source.luma_fix = _bool(table["luma_fix"], where, "luma_fix")
        if "normalize_fps" in table:
            source.normalize_fps = _bool(table["normalize_fps"], where, "normalize_fps")

        return source


#: The order a source's transformations are applied in, and why it is this way.
#:
#: Taken from squash's multi_comps.vpy, which got it right. Two of these are
#: load-bearing rather than arbitrary:
#:
#: - trim before resize/crop, because trim values are frame indices the user
#:   read off a previewer showing the untransformed clip.
#: - tonemap after crop, because peak-brightness detection averages over the
#:   frame and letterbox bars drag the measured peak down, which visibly
#:   changes the result on scope-ratio material.
PROCESSING_ORDER = (
    "normalize_fps",
    "trim",
    "resize",
    "crop",
    "tonemap",
    "luma_fix",
)


@dataclass(slots=True)
class FrameSelection:
    method: FrameMethod = FrameMethod.COUNT
    count: int = 12
    interval_seconds: float = 300.0
    manual: tuple[int, ...] = ()
    skip_start: float = 0.05
    skip_end: float = 0.10
    b_frames_only: bool = True
    skip_dark: bool = True
    seed: int | None = None

    _KEYS = {
        "method",
        "count",
        "interval_seconds",
        "manual",
        "skip_start",
        "skip_end",
        "b_frames_only",
        "skip_dark",
        "seed",
    }

    @classmethod
    def parse(cls, table: dict[str, Any]) -> FrameSelection:
        where = "[frames]"
        _unknown_keys(table, cls._KEYS, where)
        selection = cls()

        if "method" in table:
            selection.method = _enum(table["method"], FrameMethod, where, "method")
        if "count" in table:
            selection.count = _int(table["count"], where, "count", minimum=1)
        if "interval_seconds" in table:
            value = table["interval_seconds"]
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise ConfigError(f"{where}: 'interval_seconds' must be a positive number")
            selection.interval_seconds = float(value)
        if "manual" in table:
            manual = table["manual"]
            if not isinstance(manual, list) or not manual:
                raise ConfigError(f"{where}: 'manual' must be a non-empty list of frame numbers")
            frames = []
            for item in manual:
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise ConfigError(
                        f"{where}: 'manual' entries must be frame numbers >= 0, got {item!r}"
                    )
                frames.append(item)
            selection.manual = tuple(sorted(set(frames)))
        if "skip_start" in table:
            selection.skip_start = _fraction(table["skip_start"], where, "skip_start")
        if "skip_end" in table:
            selection.skip_end = _fraction(table["skip_end"], where, "skip_end")
        if "b_frames_only" in table:
            selection.b_frames_only = _bool(table["b_frames_only"], where, "b_frames_only")
        if "skip_dark" in table:
            selection.skip_dark = _bool(table["skip_dark"], where, "skip_dark")
        if "seed" in table:
            selection.seed = _int(table["seed"], where, "seed")

        if selection.method is FrameMethod.MANUAL and not selection.manual:
            raise ConfigError(
                f"{where}: method = 'manual' needs a 'manual' list of frame numbers. "
                f"Use 'kiyas pick' to choose them in a player."
            )
        return selection


@dataclass(slots=True)
class Project:
    mode: Mode
    title: str
    sources: list[Source]
    frames: FrameSelection
    engine: Engine = Engine.AUTO
    output: Path = Path("out")
    index_dir: Path | None = None
    tools: dict[str, str] = field(default_factory=dict)
    source_path: Path | None = None

    _KEYS = {"mode", "title", "engine", "source", "frames", "output", "tools"}

    @property
    def source_names(self) -> list[str]:
        return [source.name for source in self.sources]


def _parse_output(table: Any) -> tuple[Path, Path | None]:
    """Return ``(directory, index_dir)``."""
    if not isinstance(table, dict):
        raise ConfigError("[output]: must be a table")
    _unknown_keys(table, {"directory", "index_dir"}, "[output]")

    directory = table.get("directory", "out")
    if not isinstance(directory, str) or not directory.strip():
        raise ConfigError("[output]: 'directory' must be a non-empty string")

    index_dir = None
    if "index_dir" in table:
        raw = table["index_dir"]
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigError("[output]: 'index_dir' must be a non-empty string")
        index_dir = Path(raw.strip()).expanduser()

    return Path(directory.strip()).expanduser(), index_dir


def _parse_tools(table: Any) -> dict[str, str]:
    if not isinstance(table, dict):
        raise ConfigError("[tools]: must be a table")
    known = {"ffmpeg", "ffprobe", "mpv", "mediainfo", "dovi_tool", "mkvextract"}
    _unknown_keys(table, known, "[tools]")
    tools = {}
    for name, value in table.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"[tools]: '{name}' must be a path string")
        tools[name] = value.strip()
    return tools


def parse(data: dict[str, Any], *, source_path: Path | None = None) -> Project:
    """Build a :class:`Project` from already-decoded TOML."""
    if not isinstance(data, dict):
        raise ConfigError("the project file must be a TOML table")

    _unknown_keys(data, Project._KEYS, "project")

    mode = _enum(data.get("mode", "source"), Mode, "project", "mode")

    title = data.get("title", "")
    if not isinstance(title, str):
        raise ConfigError("project: 'title' must be a string")

    raw_sources = data.get("source", [])
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError(
            "project: at least one [[source]] is required. A comparison needs something to compare."
        )
    sources = [Source.parse(table, index) for index, table in enumerate(raw_sources, start=1)]

    if mode is Mode.SOURCE and len(sources) < 2:
        raise ConfigError(
            "project: a source comparison needs at least two [[source]] entries. "
            "For one file rendered several ways, use mode = 'settings'."
        )

    duplicates = sorted(
        {n for n in (s.name for s in sources) if [x.name for x in sources].count(n) > 1}
    )
    if duplicates:
        raise ConfigError(
            f"project: source names must be unique, but {', '.join(repr(d) for d in duplicates)} "
            f"appears more than once. Names become the comparison's column labels."
        )

    frames_table = data.get("frames", {})
    if not isinstance(frames_table, dict):
        raise ConfigError("[frames]: must be a table")

    output, index_dir = _parse_output(data.get("output", {}))
    return Project(
        mode=mode,
        title=title.strip(),
        sources=sources,
        frames=FrameSelection.parse(frames_table),
        engine=_enum(data.get("engine", "auto"), Engine, "project", "engine"),
        output=output,
        index_dir=index_dir,
        tools=_parse_tools(data.get("tools", {})),
        source_path=source_path,
    )


def load(path: str | Path) -> Project:
    """Read and validate a project file.

    Relative source and output paths are resolved against the project file's
    own directory, not the working directory: a project file is meant to be
    runnable from anywhere, including a GUI whose working directory is
    wherever it happened to be launched from.
    """
    path = Path(path).expanduser()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: file is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    try:
        project = parse(data, source_path=path)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    base = path.parent
    for source in project.sources:
        if not source.path.is_absolute():
            source.path = (base / source.path).resolve()
    if not project.output.is_absolute():
        project.output = (base / project.output).resolve()
    if project.index_dir is not None and not project.index_dir.is_absolute():
        project.index_dir = (base / project.index_dir).resolve()

    return project
