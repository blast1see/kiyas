"""Installing the VapourSynth stack into the environment kiyas runs in.

Since VapourSynth R74 the whole stack is pip-installable, and every plugin
wheel drops its binary into ``<site-packages>/vapoursynth/plugins``. That is
what makes the "does not touch the system" promise keepable: with a virtualenv,
the entire frame-generation stack lives in one deletable directory.

Two things are deliberately *not* done here:

``vapoursynth register-install`` / ``register-legacy-install`` / ``register-vfw``
    These write to ``HKCU\\SOFTWARE\\VapourSynth`` and register a VFW provider
    system-wide. They exist for people who want one blessed system-wide
    VapourSynth; they are exactly wrong for a self-contained tool.

Installing into a non-virtualenv interpreter
    Refused by default. See :func:`ensure_isolated`.

``vapoursynth config`` *is* run. It writes a single file,
``%APPDATA%/vapoursynth/vapoursynth.toml``, keyed by this environment's own
``vsscript.dll`` path, and merges rather than overwrites -- so several
VapourSynth installations coexist. Without it, VSScript cannot find the
interpreter and the engine fails at load time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, requires

_NOT_VENV = (
    "Refusing to install: this interpreter is not a virtual environment.\n"
    "kiyas installs VapourSynth and its plugins into the environment it runs in. "
    "Doing that to a system Python would affect every other project on this "
    "machine. Create one first, e.g.:\n"
    "    py -3.13 -m venv .venv\n"
    "    .\\.venv\\Scripts\\Activate.ps1\n"
    "Set KIYAS_ALLOW_GLOBAL_INSTALL=1 to override."
)

_CONFIG_NOTE = (
    "This writes one file, %APPDATA%\\vapoursynth\\vapoursynth.toml, which maps this "
    "environment's vsscript library to this interpreter. It is additive: other "
    "VapourSynth installations keep their own entries. No registry keys are created."
)

#: Used only when kiyas' own metadata cannot be read (running from a source
#: checkout that was never installed). Keep in sync with the ``vs`` extra in
#: pyproject.toml -- the test suite checks that they agree.
_FALLBACK_VS_PACKAGES = (
    "vapoursynth>=78",
    "vapoursynth-lsmas",
    "vapoursynth-bestsource",
    "vapoursynth-fillborders",
    "vapoursynth-vivtc",
    "vapoursynth-subtext",
    "vs-placebo",
    "vsfpng",
    "awsmfunc",
    "vstools",
)


class SetupError(RuntimeError):
    """Raised when the environment cannot be prepared."""


def is_virtualenv() -> bool:
    """True when the running interpreter is a venv or virtualenv.

    ``sys.base_prefix`` differs from ``sys.prefix`` inside a venv; the
    ``real_prefix`` attribute covers the legacy virtualenv package.
    """
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix")


def is_frozen() -> bool:
    """Whether this is the packaged build rather than a checkout."""
    return bool(getattr(sys, "frozen", False))


def ensure_isolated() -> None:
    """Refuse to install into a system interpreter unless told otherwise.

    Installing VapourSynth plus its plugins into a system Python is not
    reversible in any convenient way, and this machine has four interpreters
    that other projects depend on. The override exists for CI images and
    containers, where the whole filesystem is already disposable.
    """
    if is_frozen():
        raise SetupError(_FROZEN)
    if is_virtualenv():
        return
    if os.environ.get("KIYAS_ALLOW_GLOBAL_INSTALL") == "1":
        return
    raise SetupError(_NOT_VENV)


#: What to say when someone runs `kiyas setup` from the packaged build.
#:
#: The generic "make a virtualenv" advice is not just unhelpful there, it is
#: impossible to follow: there is no pip and no interpreter to point it at. The
#: honest answer is that the packaged build has the engines it has.
_FROZEN = """\
This is the packaged build of kiyas, which cannot install anything.

VapourSynth is a stack of compiled plugins that installs into a Python
environment, and there is none here. The packaged build has the ffmpeg and mpv
engines, which is enough for source comparisons, settings comparisons and
audio.

For the VapourSynth engine, use a checkout instead:
    git clone https://github.com/blast1see/kiyas
    cd kiyas
    .\\bootstrap.ps1"""


def vs_packages() -> list[str]:
    """The requirement strings for the ``vs`` extra.

    Read from kiyas' own installed metadata so pyproject.toml stays the single
    source of truth.
    """
    try:
        declared = requires("kiyas") or []
    except PackageNotFoundError:
        return list(_FALLBACK_VS_PACKAGES)

    packages: list[str] = []
    for entry in declared:
        requirement, _, marker = entry.partition(";")
        if "extra" in marker and "'vs'" in marker.replace('"', "'"):
            packages.append(requirement.strip())
    return packages or list(_FALLBACK_VS_PACKAGES)


def _run(args: list[str], *, description: str) -> None:
    """Run a subprocess, streaming its output, and raise on failure.

    The one subprocess in kiyas that does not pass ``no_window_flag()``, and
    the exception is the point: this inherits the console on purpose so pip's
    progress appears while it installs. The flag would give the child a console
    of its own instead of the parent's, and with nothing redirected that output
    would go nowhere. It costs nothing in a windowed build either, because a
    frozen build refuses to run setup at all -- there is no pip to call.
    """
    proc = subprocess.run(args, check=False)  # noqa: S603 - args are built here
    if proc.returncode != 0:
        raise SetupError(f"{description} failed with exit code {proc.returncode}. Output above.")


def install_vapoursynth(*, upgrade: bool = False) -> None:
    packages = vs_packages()
    args = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        args.append("--upgrade")
    args.extend(packages)
    _run(args, description="pip install")


def configure_vapoursynth() -> None:
    """Run ``vapoursynth config`` in a subprocess.

    Invoked through ``-c`` rather than the console script so it works even when
    the environment's Scripts directory is not on PATH, which is the normal
    state when kiyas is driven from a GUI or a task runner.
    """
    _run(
        [
            sys.executable,
            "-c",
            "from vapoursynth._cli import main; raise SystemExit(main(['config']))",
        ],
        description="vapoursynth config",
    )


def run(*, upgrade: bool = False) -> int:
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    ensure_isolated()

    console.print(f"Installing the VapourSynth stack into {escape(sys.prefix)}")
    packages = vs_packages()
    console.print(f"Installing {len(packages)} packages (this downloads roughly 300-500 MB)...")
    install_vapoursynth(upgrade=upgrade)

    console.print()
    console.print("Writing the VapourSynth runtime configuration...")
    console.print(f"[dim]{escape(_CONFIG_NOTE)}[/dim]")
    configure_vapoursynth()

    console.print()
    console.print("[green]Done. Run 'kiyas doctor' to confirm.[/green]")
    return 0
