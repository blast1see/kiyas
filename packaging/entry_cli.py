"""Frozen entry point for the command line.

PyInstaller needs a script, not a console-script name, and it needs the two
executables to have different scripts so it can tell them apart. Both are three
lines that hand straight over to the real entry points.
"""

from __future__ import annotations

import sys

from kiyas.cli import main

if __name__ == "__main__":
    sys.exit(main())
