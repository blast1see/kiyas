"""Environment diagnosis.

``kiyas doctor`` answers one question: what can this machine actually do right
now, and what is the shortest path to the missing pieces? It must run on a bare
interpreter with nothing but ``rich`` installed, because an environment check
that needs the environment to be set up first is worthless.

Nothing here imports the engines. Importing VapourSynth is itself one of the
things that can fail, so it is done defensively and reported, never propagated.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .media import binaries

_INSTALL_VS = "Run 'kiyas setup' to install the VapourSynth stack into this environment."
_INSTALL_FFMPEG = (
    "Install FFmpeg and put its bin directory on PATH, or set it under [tools] in the config."
)
_INSTALL_MPV = (
    "Needed for settings comparisons (tone-mapping curves, shaders, scalers) and the frame "
    "picker. Install mpv and put it on PATH, or set it under [tools] in the project file."
)
_INSTALL_AUDIO = "Run 'pip install kiyas[audio]' for audio analysis."
_INSTALL_GUI = "Run 'pip install kiyas[gui]' for the desktop interface."
_INSTALL_SYNC = (
    "Optional. For accurate offset measurement:\n"
    "pip install git+https://github.com/blast1see/AudioSyncTool\n"
    "Without it kiyas falls back to a coarse correlation."
)


class Status(Enum):
    OK = "ok"
    PARTIAL = "partial"
    WARN = "warning"
    MISSING = "missing"

    @property
    def style(self) -> str:
        return {
            Status.OK: "green",
            Status.PARTIAL: "yellow",
            Status.WARN: "yellow",
            Status.MISSING: "red",
        }[self]


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str = ""
    hint: str = ""


@dataclass(slots=True)
class Report:
    engines: list[Check] = field(default_factory=list)
    tools: list[Check] = field(default_factory=list)
    packages: list[Check] = field(default_factory=list)

    @property
    def all_checks(self) -> list[Check]:
        return [*self.engines, *self.tools, *self.packages]

    @property
    def usable_engines(self) -> list[str]:
        return [c.name for c in self.engines if c.status in (Status.OK, Status.PARTIAL)]

    @property
    def default_engine(self) -> str | None:
        """The engine kiyas would pick for a source comparison.

        Order is quality-first, not availability-first. VapourSynth addresses
        frames by index and has the best tonemapping. ffmpeg is the
        always-available fallback. mpv is last because it is a player: its
        exact seek does land on the right frame, but the picture then has to
        survive a render, a window size and a display -- three things a
        comparison of two files does not want in the way. It is not last for a
        settings comparison, where it is the only engine that can do the job.
        """
        for preferred in ("vapoursynth", "ffmpeg", "mpv"):
            if preferred in self.usable_engines:
                return preferred
        return None


# --------------------------------------------------------------------------
# VapourSynth
# --------------------------------------------------------------------------

#: VapourSynth plugin namespace -> the pip distribution that provides it.
#: The namespace is what a script actually calls (``core.lsmas.LWLibavSource``),
#: which is why the check is written against namespaces rather than package
#: names -- a package can install successfully and still fail to register.
_VS_PLUGINS: dict[str, str] = {
    "lsmas": "vapoursynth-lsmas",
    "bs": "vapoursynth-bestsource",
    "placebo": "vs-placebo",
    "fpng": "vsfpng",
    "sub": "vapoursynth-subtext",
    "fb": "vapoursynth-fillborders",
    "vivtc": "vapoursynth-vivtc",
}

#: Plugins without which a source comparison cannot be produced at all.
_VS_REQUIRED = ("placebo",)

#: At least one indexer is needed to open a file.
_VS_INDEXERS = ("lsmas", "bs")

_VS_PY_MODULES = ("awsmfunc", "vstools")


def _vapoursynth_version(vs, core) -> str:
    """Best-effort version string.

    Deliberately cannot fail the check. The attribute has moved twice already:
    ``core.version_string()`` never existed in R78, ``core.version()`` and
    ``core.version_number()`` are both deprecated in favour of
    ``core.core_version``. A working VapourSynth that merely renamed its
    version accessor must not be reported as missing -- which is exactly what
    the first version of this function did.
    """
    for get in (
        lambda: str(core.core_version),
        lambda: str(vs.__version__),
        lambda: str(core).splitlines()[0],
    ):
        try:
            value = get()
        except Exception:  # noqa: BLE001 - cosmetic only
            continue
        if value:
            return value
    return "version unknown"


def check_vapoursynth() -> Check:
    if importlib.util.find_spec("vapoursynth") is None:
        return Check("vapoursynth", Status.MISSING, "not installed", _INSTALL_VS)

    try:
        import vapoursynth as vs

        core = vs.core
        # Touch the core so a broken installation fails here rather than later.
        # Enumerating plugins is the cheapest call that actually loads it.
        loaded = {plugin.namespace for plugin in core.plugins()}
    except Exception as exc:  # noqa: BLE001 - any failure here means unusable
        # The usual cause on Windows is a missing Visual C++ runtime, or a
        # vapoursynth.toml that points at a different interpreter.
        return Check(
            "vapoursynth",
            Status.MISSING,
            f"import failed: {type(exc).__name__}: {exc}",
            _INSTALL_VS,
        )

    version = _vapoursynth_version(vs, core)
    present = {ns for ns in _VS_PLUGINS if ns in loaded}
    missing = [dist for ns, dist in _VS_PLUGINS.items() if ns not in present]
    missing += [m for m in _VS_PY_MODULES if importlib.util.find_spec(m) is None]

    has_indexer = any(ns in present for ns in _VS_INDEXERS)
    has_required = all(ns in present for ns in _VS_REQUIRED)

    if not has_indexer:
        return Check(
            "vapoursynth",
            Status.MISSING,
            f"{version}; no indexer (need one of {', '.join(_VS_INDEXERS)})",
            _INSTALL_VS,
        )
    if missing or not has_required:
        return Check(
            "vapoursynth",
            Status.PARTIAL,
            f"{version}; missing: {', '.join(sorted(missing))}",
            _INSTALL_VS,
        )
    return Check("vapoursynth", Status.OK, version)


# --------------------------------------------------------------------------
# ffmpeg / mpv
# --------------------------------------------------------------------------

#: ffmpeg filters kiyas relies on, and what breaks without each.
_FFMPEG_FILTERS = {
    "zscale": "SDR conversion",
    "libplacebo": "HDR tonemapping",
    "showspectrumpic": "audio spectrograms",
    "showwavespic": "audio waveforms",
}


def _ffmpeg_filters(path: Path) -> set[str]:
    try:
        proc = subprocess.run(  # noqa: S603
            [str(path), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    found: set[str] = set()
    for line in proc.stdout.splitlines():
        # Format: " ... name   in->out   description"
        parts = line.split()
        if len(parts) >= 2:
            found.add(parts[1])
    return found


def check_ffmpeg() -> tuple[Check, Check]:
    """Return the engine check and the raw-tool check.

    They are separate because ffprobe is needed for metadata even when the
    ffmpeg *engine* is not the one generating frames.
    """
    ffmpeg = binaries.describe("ffmpeg")
    if ffmpeg is None:
        missing = Check("ffmpeg", Status.MISSING, "not on PATH", _INSTALL_FFMPEG)
        return missing, missing

    filters = _ffmpeg_filters(ffmpeg.path)
    absent = [f for f in _FFMPEG_FILTERS if f not in filters]
    version = _short_version(ffmpeg.version) or "version unknown"

    if not filters:
        # Could not enumerate; assume usable rather than blocking the user.
        engine = Check("ffmpeg", Status.WARN, f"{version}; could not list filters")
    elif absent:
        lost = ", ".join(f"{f} ({_FFMPEG_FILTERS[f]})" for f in absent)
        engine = Check("ffmpeg", Status.PARTIAL, f"{version}; missing: {lost}")
    else:
        engine = Check("ffmpeg", Status.OK, version)

    tool = Check("ffmpeg", engine.status, str(ffmpeg.path))
    return engine, tool


#: `keybind` over IPC, which the frame picker needs, arrived in mpv 0.36.
_MPV_MINIMUM = (0, 36)

_MPV_VERSION = re.compile(r"\bv?(\d+)\.(\d+)\.(\d+)")


def _mpv_version_tuple(version: str | None) -> tuple[int, int] | None:
    match = _MPV_VERSION.search(version or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _mpv_banner(path: Path) -> str:
    """mpv's full ``--version`` output, which names the libraries it links."""
    try:
        proc = subprocess.run(  # noqa: S603
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or proc.stderr or ""


def check_mpv() -> Check:
    mpv = binaries.describe("mpv")
    if mpv is None:
        return Check("mpv", Status.MISSING, "not on PATH", _INSTALL_MPV)

    version = _short_version(mpv.version) or str(mpv.path)
    numbers = _mpv_version_tuple(mpv.version)
    if numbers is not None and numbers < _MPV_MINIMUM:
        return Check(
            "mpv",
            Status.PARTIAL,
            f"{version}; the frame picker needs {_MPV_MINIMUM[0]}.{_MPV_MINIMUM[1]} or newer",
            "Settings comparisons will still run. 'keybind' over IPC, which the picker uses "
            "to bind its keys, was added in mpv 0.36.",
        )

    banner = _mpv_banner(mpv.path)
    if banner and "libplacebo" not in banner:
        # Every tone-mapping curve and gamut-mapping mode a settings
        # comparison varies is libplacebo's. Without it mpv still plays video
        # and the variants all come out identical, which reads as a result.
        return Check(
            "mpv",
            Status.PARTIAL,
            f"{version}; built without libplacebo",
            "Tone-mapping and gamut templates need libplacebo. Shader and scaler "
            "comparisons still work.",
        )

    # The mpv engine measures frames with ffprobe rather than by seeking once
    # per candidate, so it is not usable on its own.
    if binaries.find_binary("ffprobe") is None:
        return Check(
            "mpv",
            Status.PARTIAL,
            f"{version}; ffprobe missing",
            "The mpv engine reads frame counts, picture types and brightness with ffprobe. "
            + _INSTALL_FFMPEG,
        )
    return Check("mpv", Status.OK, version)


def _short_version(version: str | None) -> str | None:
    """Trim the copyright tail that ffmpeg and friends append to their banner.

    The full line is over a hundred characters and pushes everything useful out
    of the table.
    """
    if not version:
        return None
    for marker in (" Copyright", " (C)", " (c)"):
        index = version.find(marker)
        if index > 0:
            return version[:index].strip()
    return version.strip()


# --------------------------------------------------------------------------
# Optional Python packages
# --------------------------------------------------------------------------

_OPTIONAL_PACKAGES = {
    "numpy": ("audio", _INSTALL_AUDIO),
    "scipy": ("audio", _INSTALL_AUDIO),
    "soundfile": ("audio", _INSTALL_AUDIO),
    "matplotlib": ("audio", _INSTALL_AUDIO),
    "PySide6": ("gui", _INSTALL_GUI),
    "audio_sync": ("sync", _INSTALL_SYNC),
}


def check_packages() -> list[Check]:
    checks: list[Check] = []
    for module, (extra, hint) in _OPTIONAL_PACKAGES.items():
        found = importlib.util.find_spec(module) is not None
        checks.append(
            Check(
                name=f"{module} ({extra})",
                status=Status.OK if found else Status.MISSING,
                detail="installed" if found else "not installed",
                hint="" if found else hint,
            )
        )
    return checks


def check_tools() -> list[Check]:
    """Auxiliary binaries. None of these block a basic comparison."""
    checks: list[Check] = []
    for name, purpose in (
        ("ffprobe", "media metadata"),
        ("mediainfo", "detailed audio metadata"),
        ("dovi_tool", "Dolby Vision enhancement layer"),
        ("mkvextract", "track extraction"),
    ):
        found = binaries.describe(name)
        checks.append(
            Check(
                name=name,
                status=Status.OK if found else Status.WARN,
                detail=(
                    (_short_version(found.version) or str(found.path))
                    if found
                    else f"not on PATH ({purpose})"
                ),
            )
        )
    return checks


def build_report() -> Report:
    ffmpeg_engine, ffmpeg_tool = check_ffmpeg()
    return Report(
        engines=[check_vapoursynth(), ffmpeg_engine, check_mpv()],
        tools=[ffmpeg_tool, *check_tools()],
        packages=check_packages(),
    )


def render(report: Report) -> int:
    """Print the report. Returns the process exit code."""
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    console = Console()
    console.print()
    console.rule("[bold]Environment report")
    console.print(f"[dim]Python {sys.version.split()[0]} — {sys.executable}[/dim]\n")

    for title, checks in (
        ("Frame engines", report.engines),
        ("External tools", report.tools),
        ("Python packages", report.packages),
    ):
        table = Table(title=title, title_justify="left", expand=True, title_style="bold")
        table.add_column("Component", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Detail", overflow="fold")
        for check in checks:
            # escape() matters: hints contain things like 'pip install
            # kiyas[audio]', and rich would read [audio] as a style tag and
            # silently swallow it -- turning the instruction into a wrong one.
            detail = escape(check.detail)
            if check.hint:
                hint = f"[dim]{escape(check.hint)}[/dim]"
                detail = f"{detail}\n{hint}" if detail else hint
            table.add_row(check.name, f"[{check.status.style}]{check.status.value}[/]", detail)
        console.print(table)
        console.print()

    engine = report.default_engine
    if engine is None:
        console.print(
            "[red]No frame engine is available. kiyas cannot generate screenshots yet.[/red]"
        )
        return 1
    console.print(f"[green]Ready. Default engine: {engine}[/green]")
    return 0


def run() -> int:
    return render(build_report())
