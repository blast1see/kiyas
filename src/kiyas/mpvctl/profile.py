"""The mpv configuration kiyas owns.

kiyas never reads, merges or edits the user's mpv configuration. It writes a
small profile of its own and points mpv at it with ``--config-dir``. Shader
files under the user's profile may still be *read* by absolute path when a
comparison names one -- they are inputs, like the video is.

Why a directory at all, rather than ``--no-config``: mpv looks for
``scripts/``, ``script-opts/`` and ``fonts/`` under its config directory, and
with ``--no-config`` it skips the config file but the surrounding lookups are
less predictable across versions. An empty directory of our own is the
unambiguous version of the same intent, and it gives the user somewhere to look
when they want to know what kiyas actually ran.
"""

from __future__ import annotations

from pathlib import Path

#: Written into the profile directory so nobody has to guess what it is.
MPV_CONF = """\
# Written by kiyas. Safe to delete; it is recreated on the next run.
#
# This file exists so mpv has a configuration directory that is not yours.
# kiyas invokes mpv with --config-dir pointing here, which makes mpv ignore
# your own profile entirely -- it is not read, not merged and not written.
#
# Deliberately almost empty: every render setting a comparison depends on is
# passed explicitly, so that what produced a screenshot is recorded in the
# project file rather than in ambient configuration.

# A screenshot has no use for audio, and loading a device is slow and can fail.
audio=no

# Whatever the file happens to contain must not end up burnt into the picture.
sub=no
sub-auto=no
osc=no
osd-level=0
"""

INPUT_CONF = """\
# Written by kiyas. Empty on purpose: no key does anything unless a kiyas
# command binds it. The frame picker adds its own bindings at runtime.
"""

#: Arguments every kiyas mpv invocation carries.
#:
#: ``--idle`` plus ``--pause`` means mpv comes up showing nothing and stays put
#: until it is told what to do, which is what makes a session scriptable.
#: ``--keep-open`` stops it from unloading the file when a seek lands on the
#: last frame, which would otherwise turn the final screenshot of a run into a
#: capture of an empty window.
BASE_ARGS: tuple[str, ...] = (
    "--idle=yes",
    "--pause=yes",
    "--keep-open=yes",
    "--force-window=yes",
    "--no-border",
    "--no-audio",
    "--no-sub",
    "--osc=no",
    "--osd-level=0",
    "--screenshot-format=png",
    # 8 bits per channel, like every other engine here. mpv defaults to 16-bit
    # PNGs for high-bit-depth sources, which doubles the size of every file in
    # a comparison for precision no browser will ever show. Consistency across
    # engines matters more: a project that produces 8-bit images from
    # VapourSynth and 16-bit ones from mpv is harder to reason about than one
    # that always produces the same thing.
    "--screenshot-high-bit-depth=no",
    # Window geometry in real pixels. Without this, a display running at 150%
    # scaling turns --geometry=640x360 into a 960x540 framebuffer, and the
    # comparison quietly comes out at a resolution nobody asked for.
    "--hidpi-window-scale=no",
    # Screen savers and blanking mid-run would interrupt rendering.
    "--stop-screensaver=no",
    "--msg-level=all=warn",
)


def write_profile(directory: Path) -> Path:
    """Create (or refresh) a kiyas-owned mpv profile directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mpv.conf").write_text(MPV_CONF, encoding="utf-8")
    (directory / "input.conf").write_text(INPUT_CONF, encoding="utf-8")
    # mpv scans these; existing and empty is more predictable than absent.
    for name in ("scripts", "script-opts"):
        (directory / name).mkdir(exist_ok=True)
    return directory
