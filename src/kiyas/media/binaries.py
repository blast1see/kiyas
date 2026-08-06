"""Locating and probing the external executables kiyas drives.

**The rule: absolute PATH entries only.**

``shutil.which`` is deliberately not used. Its behaviour around the current
working directory has changed between Python versions and differs per platform,
and kiyas routinely runs with the working directory set to wherever the user's
media lives. An ``ffmpeg.exe`` dropped into a release folder must never be the
thing that gets executed, so the search is written out explicitly: split PATH,
discard anything that is not an absolute directory, then look for the name plus
each PATHEXT suffix.

An explicitly configured path is honoured even outside PATH -- that is a
deliberate choice by the user rather than an ambient one -- but it still has to
be absolute and it still has to exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

# Probing a binary for its version must never be able to hang the tool. mpv in
# particular will sit there forever if it is handed the wrong arguments and the
# user's config has keep-open=yes, which is exactly how it is configured on the
# author's machine.
_PROBE_TIMEOUT = 10.0

#: name -> arguments that make it print a version and exit immediately.
_VERSION_ARGS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
    "mpv": ("--version",),
    "mediainfo": ("--Version",),
    "dovi_tool": ("--version",),
    "mkvextract": ("--version",),
    "mkvmerge": ("--version",),
}


class BinaryNotFound(RuntimeError):
    """Raised when a required executable cannot be located."""


@dataclass(frozen=True, slots=True)
class Binary:
    name: str
    path: Path
    version: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.path)


def _executable_suffixes() -> tuple[str, ...]:
    """Suffixes to try when looking for a bare command name.

    On POSIX there are none; on Windows PATHEXT decides. The empty suffix comes
    first so an extensionless file (a shim, or a WSL-style wrapper) still wins
    if it is what the user actually has.
    """
    if os.name != "nt":
        return ("",)
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    suffixes = [s for s in (p.strip() for p in pathext.split(os.pathsep)) if s]
    return ("", *suffixes)


def _search_dirs() -> list[Path]:
    """Absolute directories from PATH, in order, deduplicated.

    Relative entries -- including the empty string, which POSIX shells read as
    "the current directory" -- are dropped. That is the whole point of this
    module.
    """
    seen: set[str] = set()
    dirs: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        entry = raw.strip().strip('"')
        if not entry:
            continue
        candidate = Path(entry)
        if not candidate.is_absolute():
            continue
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        dirs.append(candidate)
    return dirs


def find_binary(name: str, configured: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the resolved path for ``name``, or ``None`` if it is not available.

    ``configured`` is an explicit override from the user's config file.
    """
    if configured is not None:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise BinaryNotFound(
                f"Refusing to run {name} from a relative path ({candidate}). Binaries are "
                f"resolved from absolute paths only, so that an executable sitting next to "
                f"your media files can never be picked up."
            )
        if candidate.is_file():
            return candidate
        raise BinaryNotFound(f"{name} was configured as {candidate}, but that file does not exist.")

    suffixes = _executable_suffixes()
    for directory in _search_dirs():
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix}"
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                # An unreadable or disconnected PATH entry (a stale network
                # drive is the usual case) must not abort the whole search.
                continue
    return None


def require_binary(name: str, configured: str | os.PathLike[str] | None = None) -> Path:
    """Like :func:`find_binary` but raises instead of returning ``None``."""
    path = find_binary(name, configured)
    if path is None:
        raise BinaryNotFound(f"{name} was not found on PATH.")
    return path


def probe_version(path: Path, name: str | None = None) -> str | None:
    """Return the first line of the binary's version output, or ``None``.

    Failure is not an error: a tool that refuses to report a version can still
    be perfectly usable, and `doctor` should say "present, version unknown"
    rather than "missing".
    """
    args = _VERSION_ARGS.get(name or path.stem.lower(), ("--version",))
    try:
        proc = subprocess.run(  # noqa: S603 - path is resolved, args are literals
            [str(path), *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=_no_window_flag(),
        )
    except (OSError, subprocess.SubprocessError):
        return None

    output = proc.stdout or proc.stderr or ""
    first = output.strip().splitlines()
    return first[0].strip() if first else None


def _no_window_flag() -> int:
    """Keep console windows from flashing when the GUI shells out."""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


@cache
def _cached_lookup(name: str, configured: str | None) -> Binary | None:
    path = find_binary(name, configured)
    if path is None:
        return None
    return Binary(name=name, path=path, version=probe_version(path, name))


def describe(name: str, configured: str | os.PathLike[str] | None = None) -> Binary | None:
    """Resolve ``name`` and probe its version, caching the result.

    Used by `doctor` and by the engines' availability checks, which both run
    several times per session and should not re-spawn processes each time.
    """
    return _cached_lookup(name, str(configured) if configured is not None else None)


def clear_cache() -> None:
    """Forget resolved binaries. Tests that manipulate PATH need this."""
    _cached_lookup.cache_clear()
