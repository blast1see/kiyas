"""The desktop interface.

**The GUI has no privileges.** It builds a project, writes it as TOML, and
calls the same core the command line calls. Everything it can do is reachable
from a terminal, the project file it produces is shareable and diffable, and
the core stays testable without a display.

That is not a style preference; it decides where the code goes. A feature that
would live here and nowhere else is in the wrong place, and the way to tell is
that you cannot describe it in a project file.

Qt is imported inside functions rather than at module scope, so
``kiyas doctor`` -- the command whose job is to report that PySide6 is missing
-- keeps working when it is.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Start the desktop interface."""
    from .app import main as run_app

    return run_app(argv)
