"""Run kiyas the way a windowed build runs it, and report what only shows there.

Five of the last eight releases fixed something that was invisible from a
terminal and from the test suite, and visible the moment somebody double-clicked
the shortcut:

===========  ======================================================
 0.1.7        a relative output path resolved against the wrong place
 0.1.8        a frozen build told to run pip
 0.1.10       an engine offered that the machine cannot run
 0.1.11       ``sys.stderr.flush()`` on a stream that is None
 0.1.13/14    a console window per subprocess, hundreds per comparison
===========  ======================================================

They share a cause. A GUI entry point has **no console**, so Python leaves
``sys.stdout`` and ``sys.stderr`` unset and every console child that wants a
console makes its own. Neither is true under pytest, under a terminal, or under
``pythonw`` started from a shell -- a shell hands its own handles down, so even
that looks like a console session.

The only way to get the real thing is to have Explorer start the process, which
is what a shortcut does. That is what this script arranges: it writes a probe,
points a shortcut at ``pythonw.exe``, asks Explorer to open it, and reads the
report the probe leaves behind.

Usage::

    python packaging/windowed_check.py path/to/project.toml [--engine ffmpeg]

It needs a real project and real media, so it is not part of the suite and does
not run in CI. It belongs to the by-hand sweep -- except that it is no longer by
hand.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

#: How long to wait for the probe to write its report. A comparison against a
#: 4K remux takes minutes; this only has to be longer than a real run.
_PATIENCE = 3600.0

#: How often to look for console windows. They last milliseconds, so a single
#: look after the fact sees nothing -- which is how 0.1.13 concluded that
#: ffmpeg was innocent.
_POLL = 0.02


PROBE = """\
import ctypes, json, sys, threading, time, traceback
from ctypes import wintypes
from pathlib import Path

REPORT = Path(r"{report}")
PROJECT = r"{project}"
ENGINE = {engine!r}

user32 = ctypes.windll.user32
_Enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
# A console window belongs to conhost, not to the program that owns the
# console, so this looks for the window classes rather than for a process.
_CONSOLE = ("ConsoleWindowClass", "PseudoConsoleWindow", "CASCADIA_HOSTING_WINDOW_CLASS")


def consoles():
    found = set()

    def visit(handle, _):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, name, 256)
        if name.value in _CONSOLE and user32.IsWindowVisible(handle):
            found.add(handle)
        return True

    user32.EnumWindows(_Enum(visit), 0)
    return found


report = {{
    "console_at_start": bool(ctypes.windll.kernel32.GetConsoleWindow()),
    "stdout_is_none": sys.stdout is None,
    "stderr_is_none": sys.stderr is None,
}}

appeared, stop = set(), threading.Event()
baseline = set()


def watch():
    while not stop.is_set():
        appeared.update(consoles() - baseline)
        time.sleep({poll})


try:
    from kiyas import config, run

    project = config.load(PROJECT)
    if ENGINE:
        project.engine = config.Engine(ENGINE)

    threading.Thread(target=watch, daemon=True).start()
    time.sleep(0.5)
    baseline = consoles()
    appeared.clear()

    started = time.time()
    progress = []
    result = run.run(project, progress=progress.append)
    time.sleep(0.8)

    report.update(
        ok=True,
        engine=result.engine,
        images=result.image_count,
        seconds=round(time.time() - started),
        warnings=list(result.warnings),
        progress_lines=len(progress),
        consoles=len(appeared),
    )
except BaseException:
    report.update(ok=False, error=traceback.format_exc())

stop.set()
REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
"""


def _pythonw() -> Path:
    """The console-less interpreter beside the one running this."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if not candidate.is_file():
        raise SystemExit(f"no pythonw.exe beside {sys.executable}; this check is Windows-only")
    return candidate


def _open_through_explorer(shortcut: Path, target: Path, argument: Path) -> None:
    """Make a shortcut and have Explorer open it.

    Explorer is the point. Starting ``pythonw`` from a shell passes the shell's
    handles down, and the child then has a perfectly good stderr -- which is
    the condition this check exists to avoid reproducing.
    """
    script = textwrap.dedent(f"""
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut('{shortcut}')
        $link.TargetPath = '{target}'
        $link.Arguments = '{argument}'
        $link.Save()
        Start-Process explorer.exe -ArgumentList '{shortcut}'
    """)
    subprocess.run(  # noqa: S603 - paths are ours
        ["powershell.exe", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", type=Path, help="A project file to run.")
    parser.add_argument("--engine", help="Override the project's engine.")
    args = parser.parse_args(argv)

    if not args.project.is_file():
        print(f"no such project: {args.project}")
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="kiyas-windowed-"))
    report = workspace / "report.json"
    probe = workspace / "probe.py"
    probe.write_text(
        PROBE.format(report=report, project=args.project.resolve(), engine=args.engine, poll=_POLL),
        encoding="utf-8",
    )

    print(f"running {args.project.name} with no console, through Explorer...")
    _open_through_explorer(workspace / "run.lnk", _pythonw(), probe)

    deadline = time.time() + _PATIENCE
    while not report.is_file():
        if time.time() > deadline:
            print(f"the probe wrote nothing within {_PATIENCE / 60:.0f} minutes")
            return 1
        time.sleep(2)

    import json

    found = json.loads(report.read_text(encoding="utf-8"))
    return _render(found)


def _render(found: dict) -> int:
    print()
    print(f"  console at start : {found['console_at_start']}")
    print(f"  sys.stdout None  : {found['stdout_is_none']}")
    print(f"  sys.stderr None  : {found['stderr_is_none']}")

    if not found["stderr_is_none"]:
        # Without this the check proves nothing: it ran in a process that had a
        # console after all, which is the state everything already works in.
        print("\n  NOT A WINDOWED PROCESS -- this run cannot show the bugs it looks for")
        return 1

    if not found.get("ok"):
        print("\n  the run raised:\n")
        print(textwrap.indent(found.get("error", "no detail"), "    "))
        return 1

    print(f"  engine           : {found['engine']}")
    print(f"  images           : {found['images']} in {found['seconds']}s")
    print(f"  console windows  : {found['consoles']}")
    for warning in found["warnings"]:
        print(f"\n  warning: {textwrap.fill(warning, 88, subsequent_indent='           ')}")

    if found["consoles"]:
        print(f"\n  FAILED: {found['consoles']} console window(s) appeared during the run")
        return 1
    if not found["images"]:
        print("\n  FAILED: the run produced no images")
        return 1
    print("\n  passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
