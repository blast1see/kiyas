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

from .mpvctl.variants import TEMPLATES, Variant, VariantError, expand_template, normalise_options


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


class DoviEl(Enum):
    """What to do with a profile 7 Dolby Vision enhancement layer."""

    OFF = "off"
    AUTO = "auto"
    ON = "on"


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
    dovi_el: DoviEl = DoviEl.AUTO

    _KEYS = {
        "path",
        "name",
        "trim",
        "crop",
        "resize",
        "tonemap",
        "luma_fix",
        "normalize_fps",
        "dovi_el",
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
            # No negative trims. `clip[-5:]` is valid Python and takes the last
            # five frames of the film, which is not a comparison of anything.
            source.trim = _int(table["trim"], where, "trim", minimum=0)
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
        if "dovi_el" in table:
            source.dovi_el = _enum(table["dovi_el"], DoviEl, where, "dovi_el")

        # The enhancement layer is composed inside the Dolby Vision tone-mapping
        # pass -- vs-placebo takes it as an argument to that one call -- so there
        # is no way to bake it and then tone map some other way. This is a
        # contradiction in the file itself, so it is caught here rather than
        # after a forty-minute extraction.
        if source.dovi_el is DoviEl.ON and source.tonemap not in (Tonemap.AUTO, Tonemap.DOVI):
            raise ConfigError(
                f"{where}: dovi_el = 'on' needs tonemap = 'dovi' or 'auto', but this source "
                f"sets tonemap = '{source.tonemap.value}'. The enhancement layer is composed "
                f"inside the Dolby Vision tone-mapping pass, so it cannot be baked and then "
                f"mapped some other way. Change one of the two lines."
            )

        # 'dovi_el' and 'resize' cannot both apply to the same clip either, but
        # saying so here would reject a resized SDR source: whether the file has
        # a layer at all is not knowable without probing it. The engine raises
        # that one, where it knows what the file is.
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
#: - dovi_el immediately before tonemap, because the two happen in one pass:
#:   vs-placebo composes the enhancement layer inside the same Tonemap call
#:   that maps the result, and there is no way to ask for one without the
#:   other. It sits after crop because cropping both layers identically keeps
#:   them aligned, and resize is refused alongside it for the opposite reason.
PROCESSING_ORDER = (
    "normalize_fps",
    "trim",
    "resize",
    "crop",
    "dovi_el",
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
    dark: int = 0
    light: int = 0
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
        "dark",
        "light",
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
        if "dark" in table:
            selection.dark = _int(table["dark"], where, "dark", minimum=0)
        if "light" in table:
            selection.light = _int(table["light"], where, "light", minimum=0)
        if "seed" in table:
            selection.seed = _int(table["seed"], where, "seed")

        if selection.method is FrameMethod.MANUAL and not selection.manual:
            raise ConfigError(
                f"{where}: method = 'manual' needs a 'manual' list of frame numbers. "
                f"Use 'kiyas pick' to choose them in a player."
            )
        return selection


@dataclass(slots=True)
class Settings:
    """A settings comparison: one file, rendered several ways.

    ``width`` is the capture width in pixels; the height follows the source's
    aspect ratio so no letterbox bars end up in the picture. mpv renders into a
    window and a window cannot exceed the display, so a 4K source captures at
    less than 4K unless ``fullscreen`` is set -- and even then, at the screen's
    resolution. The size actually produced is reported after the run.
    """

    variants: list[Variant]
    width: int | None = None
    fullscreen: bool = False

    _KEYS = {"template", "width", "fullscreen", "base", "shaders"}

    @classmethod
    def parse(cls, table: Any, variant_tables: Any) -> Settings:
        where = "[settings]"
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: must be a table")
        _unknown_keys(table, cls._KEYS, where)

        try:
            base = normalise_options(table.get("base", {}), f"{where}.base")
        except VariantError as exc:
            raise ConfigError(str(exc)) from None

        variants: list[Variant] = []
        if "template" in table:
            name = table["template"]
            if not isinstance(name, str) or not name.strip():
                raise ConfigError(f"{where}: 'template' must be the name of a template")
            try:
                variants.extend(expand_template(name.strip(), table))
            except VariantError as exc:
                raise ConfigError(f"{where}: {exc}") from None

        if variant_tables is not None:
            if not isinstance(variant_tables, list):
                raise ConfigError("project: [[variant]] entries must be tables")
            for index, entry in enumerate(variant_tables, start=1):
                variants.append(_parse_variant(entry, index))

        if len(variants) < 2:
            raise ConfigError(
                "a settings comparison needs at least two variants. Add [[variant]] entries, "
                f"or set a template in [settings] (available: {', '.join(sorted(TEMPLATES))})."
            )

        names = [v.name for v in variants]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ConfigError(
                f"project: variant names must be unique, but "
                f"{', '.join(repr(d) for d in duplicates)} appears more than once. "
                f"Names become the comparison's column labels."
            )

        settings = cls(variants=[v.merged(base) for v in variants])
        if "width" in table:
            settings.width = _int(table["width"], where, "width", minimum=16)
        if "fullscreen" in table:
            settings.fullscreen = _bool(table["fullscreen"], where, "fullscreen")
        return settings

    def resolve_shaders(self, base: Path) -> None:
        """Make shader paths absolute, relative to the project file.

        Shader files are read where they live and are never copied or edited.
        Checking they exist here rather than letting mpv fail at startup is
        worth the few lines: mpv's complaint about a missing shader arrives as
        one line of player log inside a failed capture, and a typo in a path is
        much easier to see spelled out.
        """
        for variant in self.variants:
            raw = variant.options.get("glsl-shaders")
            if not raw:
                continue
            resolved = []
            for entry in raw.split(","):
                if not entry.strip():
                    continue
                path = Path(entry.strip()).expanduser()
                if not path.is_absolute():
                    path = (base / path).resolve()
                if not path.is_file():
                    raise ConfigError(f"variant {variant.name!r}: no shader at {path}")
                resolved.append(path.as_posix())
            variant.options["glsl-shaders"] = ",".join(resolved)


def _parse_variant(table: Any, index: int) -> Variant:
    where = f"variant #{index}"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: each [[variant]] must be a table")
    _unknown_keys(table, {"name", "options"}, where)

    name = table.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(
            f"{where}: 'name' is required. It labels the column in the published comparison, "
            f"so 'st2094-40' is worth more than 'variant 3'."
        )
    try:
        options = normalise_options(table.get("options", {}), f"{where} ({name.strip()!r})")
    except VariantError as exc:
        raise ConfigError(str(exc)) from None
    if not options:
        raise ConfigError(
            f"{where} ({name.strip()!r}): 'options' is required and must set at least one mpv "
            f"option. A variant that changes nothing renders the same picture as its neighbour."
        )
    return Variant(name.strip(), options)


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
    settings: Settings | None = None
    source_path: Path | None = None

    _KEYS = {
        "mode",
        "title",
        "engine",
        "source",
        "frames",
        "output",
        "tools",
        "settings",
        "variant",
    }

    @property
    def source_names(self) -> list[str]:
        return [source.name for source in self.sources]

    @property
    def column_names(self) -> list[str]:
        """What the comparison's columns are called, whichever mode it is."""
        if self.settings is not None:
            return [variant.name for variant in self.settings.variants]
        return self.source_names


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
    if mode is Mode.SETTINGS and len(sources) != 1:
        raise ConfigError(
            f"project: a settings comparison renders one file several ways, so it takes exactly "
            f"one [[source]]; this one has {len(sources)}. To compare different files, use "
            f"mode = 'source'."
        )

    settings = None
    has_settings_tables = "settings" in data or "variant" in data
    if mode is Mode.SETTINGS:
        settings = Settings.parse(data.get("settings", {}), data.get("variant"))
    elif has_settings_tables:
        raise ConfigError(
            "project: [settings] and [[variant]] only mean something with mode = 'settings'. "
            "In a source comparison the columns are the files themselves."
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
    frames = FrameSelection.parse(frames_table)
    if mode is Mode.SETTINGS and frames.b_frames_only:
        # Every column is the same decoded frame, so there is no encode to
        # flatter and nothing to be fair about. Insisting on a B-frame here
        # would only throw away positions for no benefit.
        frames.b_frames_only = False

    return Project(
        mode=mode,
        title=title.strip(),
        sources=sources,
        frames=frames,
        engine=_enum(data.get("engine", "auto"), Engine, "project", "engine"),
        output=output,
        index_dir=index_dir,
        tools=_parse_tools(data.get("tools", {})),
        settings=settings,
        source_path=source_path,
    )


# --------------------------------------------------------------------------
# Writing one back out
# --------------------------------------------------------------------------


def _toml_string(value: str) -> str:
    """Quote a string for TOML, preferring a literal.

    Windows paths are full of backslashes, and in a basic TOML string every
    one of them has to be doubled. A literal string takes them as they are,
    which keeps a written project file readable by the person who has to edit
    it later. Only a value containing a single quote needs the other form.
    """
    text = str(value)
    if "'" not in text and "\n" not in text:
        return f"'{text}'"
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _percent(fraction: float) -> str:
    return f"'{fraction * 100:g}%'"


def dumps(project: Project) -> str:
    """Render a project back to TOML.

    The GUI writes project files through this, which is what keeps it from
    growing privileges: whatever it can set, it sets by writing the same file
    a person could have typed. ``parse(tomllib.loads(dumps(p)))`` gives back an
    equivalent project, and a test holds that.

    Defaults are written out rather than omitted. A file that lists what it is
    doing can be edited by someone who has never read this module; one that
    relies on defaults cannot be, without going and finding them.
    """
    lines: list[str] = ["# Written by kiyas.", ""]
    if project.title:
        lines.append(f"title = {_toml_string(project.title)}")
    lines.append(f"mode = '{project.mode.value}'")
    lines.append(f"engine = '{project.engine.value}'")
    lines.append("")

    frames = project.frames
    lines.append("[frames]")
    lines.append(f"method = '{frames.method.value}'")
    if frames.method is FrameMethod.COUNT:
        lines.append(f"count = {frames.count}")
    elif frames.method is FrameMethod.INTERVAL:
        lines.append(f"interval_seconds = {frames.interval_seconds:g}")
    else:
        lines.append(f"manual = [{', '.join(str(frame) for frame in frames.manual)}]")
    lines.append(f"skip_start = {_percent(frames.skip_start)}")
    lines.append(f"skip_end = {_percent(frames.skip_end)}")
    lines.append(f"b_frames_only = {str(frames.b_frames_only).lower()}")
    lines.append(f"skip_dark = {str(frames.skip_dark).lower()}")
    if frames.dark:
        lines.append(f"dark = {frames.dark}")
    if frames.light:
        lines.append(f"light = {frames.light}")
    if frames.seed is not None:
        lines.append(f"seed = {frames.seed}")
    lines.append("")

    for source in project.sources:
        lines.append("[[source]]")
        lines.append(f"path = {_toml_string(source.path)}")
        lines.append(f"name = {_toml_string(source.name)}")
        if source.trim:
            lines.append(f"trim = {source.trim}")
        if source.crop:
            lines.append(f"crop = [{', '.join(str(value) for value in source.crop)}]")
        if source.resize:
            lines.append(f"resize = [{source.resize[0]}, {source.resize[1]}]")
        if source.tonemap is not Tonemap.AUTO:
            lines.append(f"tonemap = '{source.tonemap.value}'")
        if source.luma_fix:
            lines.append("luma_fix = true")
        if not source.normalize_fps:
            lines.append("normalize_fps = false")
        if source.dovi_el is not DoviEl.AUTO:
            lines.append(f"dovi_el = '{source.dovi_el.value}'")
        lines.append("")

    if project.settings is not None:
        lines.append("[settings]")
        if project.settings.width:
            lines.append(f"width = {project.settings.width}")
        if project.settings.fullscreen:
            lines.append("fullscreen = true")
        lines.append("")
        # Variants are written out one by one even when a template produced
        # them. A template is a shorthand whose meaning can change between
        # versions; the file records what was actually rendered.
        for variant in project.settings.variants:
            lines.append("[[variant]]")
            lines.append(f"name = {_toml_string(variant.name)}")
            options = ", ".join(
                f"{name} = {_toml_string(value)}" for name, value in variant.options.items()
            )
            lines.append(f"options = {{ {options} }}")
            lines.append("")

    lines.append("[output]")
    lines.append(f"directory = {_toml_string(project.output)}")
    if project.index_dir is not None:
        lines.append(f"index_dir = {_toml_string(project.index_dir)}")

    if project.tools:
        lines.append("")
        lines.append("[tools]")
        lines.extend(
            f"{name} = {_toml_string(path)}" for name, path in sorted(project.tools.items())
        )

    return "\n".join(lines).rstrip() + "\n"


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
    if project.settings is not None:
        try:
            project.settings.resolve_shaders(base)
        except ConfigError as exc:
            raise ConfigError(f"{path}: {exc}") from None

    return project
