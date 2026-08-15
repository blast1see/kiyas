"""Driving mpv without touching the user's mpv.

Everything in this package invokes mpv with ``--config-dir`` pointing at a
directory kiyas owns. That is invariant #2 in CLAUDE.md and it is not a
best-effort measure: with the flag set, mpv reads *only* that directory, so
``%APPDATA%/mpv`` (or ``~/.config/mpv``) is not read, not merged and not
written. Verified by running with ``--msg-level=all=v`` and checking that no
line mentions the user's profile.

mpv is here for the one thing the frame-accurate engines cannot do: GLSL
shaders and libplacebo tone-mapping curves only exist inside a player, so a
"which settings should I use" comparison is impossible in VapourSynth by
construction.
"""

from __future__ import annotations

from .ipc import IpcError, MpvIpc
from .profile import BASE_ARGS, write_profile
from .session import MpvSession, SessionError
from .variants import TEMPLATES, Variant, expand_template

__all__ = [
    "BASE_ARGS",
    "TEMPLATES",
    "IpcError",
    "MpvIpc",
    "MpvSession",
    "SessionError",
    "Variant",
    "expand_template",
    "write_profile",
]
