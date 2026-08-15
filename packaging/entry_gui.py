"""Frozen entry point for the window. See entry_cli.py."""

from __future__ import annotations

import sys

from kiyas.gui import main

if __name__ == "__main__":
    sys.exit(main())
