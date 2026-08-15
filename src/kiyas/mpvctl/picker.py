"""Choosing frames by watching the film.

Automatic frame selection is good at spreading captures evenly and avoiding
black ones, and bad at everything else. It does not know that the grain in the
snow at 01:12:30 is the whole argument, or that the only scene worth comparing
is the one lit by a single candle. This is how you say so: play the file,
press a key on the frames you want, and get a list back.

The list is printed as a ``[frames]`` block ready to paste into a project file,
rather than written into one. A picker that edits your project file has to
guess whether it is adding to or replacing what is there, and getting that
wrong destroys work; printing it cannot.
"""

from __future__ import annotations

import time
from fractions import Fraction
from pathlib import Path

from .session import MpvSession, SessionError

#: The bindings, and the help text that describes them. One list so a new key
#: cannot be added without the on-screen help mentioning it.
BINDINGS: tuple[tuple[str, str, str], ...] = (
    ("s", "mark", "mark this frame"),
    ("u", "undo", "remove the last mark"),
    ("c", "clear", "remove every mark"),
)

#: mpv options the picker needs that the capture profile deliberately removes:
#: a window you can see and drive, and playback that starts when you say so.
INTERACTIVE_ARGS: tuple[str, ...] = (
    "--border",
    "--osc=yes",
    "--osd-level=1",
    "--keep-open=yes",
    "--pause=yes",
    # Frame stepping is the point, so seeking has to land where it is asked to.
    "--hr-seek=yes",
)


class PickerError(RuntimeError):
    """Raised when the picker cannot run."""


def _help_text() -> str:
    lines = "   ".join(f"{key}: {what}" for key, _, what in BINDINGS)
    return f"kiyas frame picker -- {lines}   q: finish"


def pick(
    mpv: Path,
    path: Path,
    *,
    config_dir: Path,
    fps: Fraction,
    start_frame: int = 0,
    poll: float = 0.1,
) -> list[int]:
    """Play ``path`` and return the frames the user marked, in order.

    ``fps`` comes from the same probe the rest of kiyas uses rather than from
    mpv, so a frame number chosen here means the same thing to the engine that
    will capture it.
    """
    try:
        session = MpvSession(
            mpv,
            config_dir=config_dir,
            tag="picker",
            extra_args=INTERACTIVE_ARGS,
        )
    except SessionError as exc:
        raise PickerError(str(exc)) from exc

    marked: list[int] = []
    try:
        session.load(path)
        if start_frame:
            session.seek_seconds(start_frame / float(fps))

        for key, name, _ in BINDINGS:
            session.bind(key, f"script-message kiyas-{name}")
        session.show_text(_help_text(), 6000)

        while session.alive:
            for event in session.poll_events():
                if event.get("event") == "shutdown":
                    return marked
                if event.get("event") != "client-message":
                    continue
                args = event.get("args") or []
                action = str(args[0]) if args else ""
                _apply(session, action, marked, fps)
            time.sleep(poll)
    finally:
        session.close()
    return marked


def _apply(session: MpvSession, action: str, marked: list[int], fps: Fraction) -> None:
    if action == "kiyas-mark":
        position = session.get("time-pos")
        if position is None:
            session.show_text("nothing playing")
            return
        # Rounding, not truncation: a paused mpv sits exactly on a frame
        # boundary, and float arithmetic lands a hair either side of it.
        frame = max(0, round(float(position) * float(fps)))
        if frame in marked:
            session.show_text(f"frame {frame} already marked ({len(marked)} total)")
            return
        marked.append(frame)
        session.show_text(f"marked frame {frame}  ({len(marked)} total)")
    elif action == "kiyas-undo":
        if marked:
            session.show_text(f"removed frame {marked.pop()}  ({len(marked)} left)")
        else:
            session.show_text("nothing to undo")
    elif action == "kiyas-clear":
        marked.clear()
        session.show_text("cleared every mark")


def as_toml(frames: list[int]) -> str:
    """The marked frames as a ``[frames]`` block, ready to paste."""
    if not frames:
        return "# no frames were marked"
    listed = ", ".join(str(frame) for frame in sorted(set(frames)))
    return f'[frames]\nmethod = "manual"\nmanual = [{listed}]'


__all__ = ["BINDINGS", "INTERACTIVE_ARGS", "PickerError", "as_toml", "pick"]
