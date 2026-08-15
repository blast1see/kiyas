"""Starting the window.

``kiyas-gui`` lands here, and so does ``kiyas gui``. Files named on the command
line are added to the list, and a ``.toml`` is opened as a project -- so the
window can be the file association for a project file without anyone having to
write that integration.

The argument handling is a plain function rather than a step inside ``main``
because it is the only part with a decision in it, and it is worth testing
without starting an application.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .window import MainWindow


def split_arguments(arguments: list[str]) -> tuple[list[Path], list[Path]]:
    """``(projects, media)`` from what was named on the command line.

    Anything that is not a file is dropped rather than reported: the usual way
    to get one here is a shell that did not expand a pattern, and a window that
    refuses to open over it would be worse than one that opens empty.
    """
    paths = [Path(argument) for argument in arguments]
    existing = [path for path in paths if path.is_file()]
    projects = [path for path in existing if path.suffix.lower() == ".toml"]
    media = [path for path in existing if path not in projects]
    return projects, media


def apply_arguments(window: MainWindow, arguments: list[str]) -> None:
    """Put whatever was named on the command line into ``window``."""
    from .. import config

    projects, media = split_arguments(arguments)
    if projects:
        try:
            window.load_project(config.load(projects[0]))
        except config.ConfigError as exc:
            # Reported in the window rather than raised: the point of opening a
            # project this way is to get a window, and an unreadable file is
            # something to fix in it.
            window.log.appendPlainText(f"could not open {projects[0]}: {exc}")
    if media:
        window.add_paths(media)


def main(argv: list[str] | None = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "The desktop interface needs PySide6.\n"
            "Run 'pip install kiyas[gui]', or use the command line: 'kiyas --help'.",
            file=sys.stderr,
        )
        return 2

    from .window import MainWindow

    arguments = list(sys.argv[1:] if argv is None else argv)
    application = QApplication([sys.argv[0] if sys.argv else "kiyas"])
    application.setApplicationName("kiyas")

    window = MainWindow()
    apply_arguments(window, arguments)
    window.show()
    return application.exec()
