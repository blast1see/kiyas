"""Turn a tag into release notes, and refuse if the tag disagrees with the code.

Run as `python packaging/release_notes.py v0.1.0 notes.md`; without the second
argument the notes go to stdout. The file is written here rather than by a
shell redirect because the changelog has em dashes in it and the encoding a
redirect picks depends on which PowerShell the runner happens to start.

Two jobs, and the second is the important one. A release built from `v0.2.0`
whose binary answers `kiyas 0.1.0` is a bug nobody finds until someone reports
against the wrong version, so the tag is checked against `pyproject.toml`
before anything is published. An absent changelog section is the same class of
mistake: a release page with no notes is worse than a build that stops.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Sections are `## <version>`; anything deeper belongs to the section.
_HEADING = re.compile(r"^## +(?P<version>\S+)\s*$", re.MULTILINE)


def declared_version(pyproject: Path) -> str:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def section(changelog: str, version: str) -> str:
    """The body of the `## <version>` section, without its heading."""
    headings = list(_HEADING.finditer(changelog))
    for index, heading in enumerate(headings):
        if heading.group("version") != version:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
        return changelog[heading.end() : end].strip()
    raise LookupError(f"CHANGELOG.md has no section for {version}")


def notes(tag: str, *, root: Path = ROOT) -> str:
    version = tag.removeprefix("v")
    declared = declared_version(root / "pyproject.toml")
    if version != declared:
        raise ValueError(f"tag {tag} does not match the packaged version {declared}")
    return section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)


def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 3:
        print(f"usage: {Path(argv[0]).name} <tag> [output]", file=sys.stderr)
        return 2
    try:
        body = notes(argv[1])
    except (LookupError, ValueError) as problem:
        print(problem, file=sys.stderr)
        return 1
    if len(argv) == 3:
        Path(argv[2]).write_text(body + "\n", encoding="utf-8")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
