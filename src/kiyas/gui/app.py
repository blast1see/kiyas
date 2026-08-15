"""Starting the window.

``kiyas-gui`` lands here, and so does ``kiyas gui``. Files named on the command
line are added to the list, and a ``.toml`` is opened as a project -- so the
window can be the default application for a project file without anyone having
to write that integration.
"""

from __future__ import annotations

import sys
from pathlib import Path


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

    from .. import config
    from .window import MainWindow

    arguments = list(sys.argv[1:] if argv is None else argv)
    application = QApplication([sys.argv[0] if sys.argv else "kiyas"])
    application.setApplicationName("kiyas")

    window = MainWindow()
    paths = [Path(argument) for argument in arguments if Path(argument).is_file()]
    projects = [path for path in paths if path.suffix.lower() == ".toml"]
    media = [path for path in paths if path not in projects]

    if projects:
        try:
            window.load_project(config.load(projects[0]))
        except config.ConfigError as exc:
            window.log.appendPlainText(f"could not open {projects[0]}: {exc}")
    if media:
        window.add_paths(media)

    window.show()
    return application.exec()
