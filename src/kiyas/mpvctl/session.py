"""One mpv process, driven frame by frame.

The important part of this module is :meth:`MpvSession.capture`, and the reason
it is not three lines long is worth reading before changing it.

**mpv's renderer keeps showing the previous frame after a seek reports that it
finished.** ``screenshot window`` draws whatever the video output currently
holds, so a capture taken as soon as ``playback-restart`` arrives is the frame
*before* the one that was asked for -- or, on the first capture of a session,
an empty window.

This is not a rare race. Measured with one capture per seek: on a 640x360 clip
it went wrong 4 times in 48; on a 3840x2160 clip it went wrong **8 times in 8**,
every capture showing exactly the previous frame. Decoding and rendering 4K
takes longer, so the player finishes the seek further ahead of the renderer.
A black screenshot is obvious; a screenshot of the wrong frame is not, and in a
release comparison it reads as "these two sources differ here" -- the exact
conclusion the tool exists to support. Nobody would suspect the player.

**The barrier is ``vo-passes``.** It is the video output's own record of what it
has drawn: a list of render passes with timings, appended to once per rendered
frame. It is the only property found that is updated *after* the picture
exists, rather than when the player decides which picture is next.
``video-frame-info`` was tried first and looks convincing -- it even reports the
picture type of the stale frame, which is how the lag was spotted from the
other side -- but it is player-side and changes immediately, so it fixed the
640x360 case and left the 4K case wrong 8 times out of 8.

When ``vo-passes`` is unavailable -- it needs a GPU video output -- the fallback
is to wait for ``video-frame-info`` and then throw a screenshot away, since
asking the renderer for a picture makes it catch up. Measured 0 wrong in 8 on
4K, but it is a workaround rather than a statement about rendering, so the
session says it is running that way and the run reports it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path

from ..media.binaries import no_window_flag
from .ipc import IpcError, MpvIpc, endpoint
from .profile import BASE_ARGS, write_profile

#: How long to wait for a seek to complete. A cold seek into a 4K remux on a
#: spinning disk is slow; anything past this is a hang.
SEEK_TIMEOUT = 120.0

#: Encoding and writing one PNG.
SCREENSHOT_TIMEOUT = 180.0

#: How long to wait for the renderer to catch up with the player after a seek
#: has already reported success. Measured at around 100 ms for 4K; the budget
#: is generous because waiting too long is harmless and not waiting is not.
RENDER_TIMEOUT = 15.0

#: How long to wait for a caption change to be drawn. Short and non-fatal:
#: setting the caption to what it already says produces no render at all, and a
#: capture with a stale caption is a visible flaw, not a silent one.
CAPTION_TIMEOUT = 2.0

#: Lines of mpv output kept for error messages.
_LOG_LINES = 60


class SessionError(RuntimeError):
    """Raised when mpv cannot be started or cannot do what was asked."""


def build_args(
    mpv: Path,
    *,
    config_dir: Path,
    address: str,
    options: Mapping[str, str] | None = None,
    width: int | None = None,
    fullscreen: bool = False,
    label: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """The full mpv command line for a session.

    Separate from launching it so a test can assert what kiyas would run
    without running it -- in particular that ``--config-dir`` is always there,
    which is the whole of invariant #2.
    """
    args = [
        str(mpv),
        f"--config-dir={config_dir}",
        f"--input-ipc-server={address}",
        *BASE_ARGS,
    ]
    if fullscreen:
        args.append("--fullscreen")
    elif width:
        # Width only, deliberately. Giving mpv both dimensions makes it
        # letterbox anything whose aspect ratio does not match, and those bars
        # end up in the screenshot: a 2.39:1 film in a 1920x1080 window
        # captures as 1920x1080 with 138 rows of black top and bottom. With
        # just a width, the window takes the source's aspect ratio and the
        # capture is all picture.
        args.append(f"--geometry={int(width)}")
    if label is not None:
        args += ["--osd-level=1", f"--osd-msg1={label}", "--osd-font-size=30"]
    for name, value in (options or {}).items():
        args.append(f"--{name}={value}")
    # Last, so a caller that needs a different kind of window -- the frame
    # picker wants borders, controls and no pause -- can override the capture
    # defaults. mpv takes the last value of a repeated option.
    args.extend(extra_args)
    return args


class MpvSession:
    """A paused mpv, connected over IPC, ready to be seeked and screenshotted.

    One session renders with one set of options. A settings comparison starts a
    fresh process per variant rather than changing options in place: mpv does
    apply most render options at runtime, but "most" is not a property worth
    depending on when the failure mode is a screenshot that silently still has
    the previous variant's shader applied.
    """

    def __init__(
        self,
        mpv: Path,
        *,
        config_dir: Path,
        tag: str,
        options: Mapping[str, str] | None = None,
        width: int | None = None,
        fullscreen: bool = False,
        label: str | None = None,
        extra_args: Sequence[str] = (),
    ):
        write_profile(config_dir)
        self._address = endpoint(tag)
        self._closed = False
        self._current_frame: int | None = None
        self._marker: str | None = None
        self._scratch: Path | None = None
        #: True once a capture has had to fall back to the weaker barrier. The
        #: run reports it: the pictures are probably right, but "probably" is
        #: worth saying out loud in a tool whose output is evidence.
        self.best_effort = False

        args = build_args(
            mpv,
            config_dir=config_dir,
            address=self._address,
            options=options,
            width=width,
            fullscreen=fullscreen,
            label=label,
            extra_args=extra_args,
        )
        self.args = args
        self._log: deque[str] = deque(maxlen=_LOG_LINES)
        try:
            self._process = subprocess.Popen(  # noqa: S603 - path resolved, args built here
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                # PATHEXT puts .com ahead of .exe, so on Windows this resolves
                # to mpv.com -- a console wrapper that puts up a console of its
                # own. Measured from a real windowed process: without this flag
                # a visible console appears per launch, and a settings
                # comparison launches one mpv per variant.
                creationflags=no_window_flag(),
            )
        except OSError as exc:
            raise SessionError(f"could not start mpv: {exc}") from exc

        # Draining mpv's own output happens on a thread, which is safe: it is a
        # different handle from the IPC channel. The IPC channel itself is
        # single-threaded for the reason given in ipc.py.
        threading.Thread(target=self._drain, daemon=True).start()

        try:
            self._ipc = MpvIpc(self._address)
        except IpcError as exc:
            self._terminate()
            raise SessionError(f"{exc}\n{self.log_tail()}") from exc

    # -- lifecycle -------------------------------------------------------

    def _drain(self) -> None:
        stream = self._process.stdout
        if stream is None:  # pragma: no cover - always a pipe here
            return
        for line in stream:
            text = line.rstrip()
            if text:
                self._log.append(text)

    def log_tail(self) -> str:
        """Recent mpv output, for error messages."""
        return "\n".join(self._log)

    def _terminate(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn mpv
                self._process.kill()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ipc.command("quit", timeout=5)
        except IpcError:
            pass
        finally:
            self._ipc.close()
            self._terminate()
            if self._scratch is not None:
                shutil.rmtree(self._scratch.parent, ignore_errors=True)
                self._scratch = None

    def __enter__(self) -> MpvSession:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- loading ---------------------------------------------------------

    def load(self, path: Path) -> None:
        """Open a file, and wait until its first frame has been drawn.

        Waiting here is what makes the barrier on the *first* seek mean
        anything. Loading a file queues a render of frame 0; if that render is
        still outstanding when the first seek finishes, the renderer catches up
        by drawing frame 0, the barrier sees the change it was waiting for, and
        the capture is of frame 0 rather than the frame that was asked for.
        Observed exactly that way: with the barrier in place, every capture in
        a session was right except the first, which came back blank.
        """
        marker = self._render_marker()
        try:
            self._ipc.command("loadfile", str(path))
            self._ipc.wait_event("file-loaded", timeout=SEEK_TIMEOUT)
        except IpcError as exc:
            raise SessionError(f"mpv could not open {path}: {exc}\n{self.log_tail()}") from exc

        self._current_frame = None
        if self._render_marker() is None and marker is None:
            # No render record to wait on. The per-seek barrier falls back to
            # its own method and says so; there is nothing to do here.
            return

        deadline = time.monotonic() + RENDER_TIMEOUT
        while time.monotonic() < deadline:
            current = self._render_marker()
            if current != marker:
                self._marker = current
                return
            time.sleep(0.004)
        # Not fatal: the per-seek barrier still has to hold, and it does its own
        # complaining if it cannot.
        self._marker = self._render_marker()

    def get(self, name: str):
        """Read an mpv property, or ``None`` if it is unavailable."""
        return self._ipc.try_command("get_property", name)

    # -- interactive use -------------------------------------------------

    def bind(self, key: str, command: str) -> None:
        """Make a key run an mpv command. Used by the frame picker."""
        self._ipc.try_command("keybind", key, command)

    def seek_seconds(self, seconds: float) -> None:
        """Move the playhead, without waiting for a render.

        For the picker, where a person is about to look at the window anyway.
        Captures go through :meth:`capture`, which does wait.
        """
        self._ipc.try_command("seek", max(0.0, seconds), "absolute+exact", timeout=SEEK_TIMEOUT)

    def show_text(self, text: str, milliseconds: int = 1500) -> None:
        self._ipc.try_command("show-text", text, milliseconds)

    def poll_events(self) -> list[dict]:
        """Everything mpv has reported since the last call."""
        return self._ipc.drain_events()

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    @property
    def capture_size(self) -> tuple[int, int] | None:
        """Pixel size of what ``screenshot window`` will produce, if known."""
        osd = self.get("osd-dimensions")
        if isinstance(osd, dict) and osd.get("w") and osd.get("h"):
            return int(osd["w"]), int(osd["h"])
        return None

    # -- capture ---------------------------------------------------------

    def _frame_info(self):
        return self._ipc.try_command("get_property", "video-frame-info")

    def _render_marker(self) -> str | None:
        """A fingerprint of what the video output has drawn, or ``None``.

        ``vo-passes/fresh`` is the list of render passes for the last *new*
        frame, each carrying its timings. A redraw of the same picture -- an
        OSD change, a window resize -- lands in ``vo-passes/redraw`` instead,
        so this only moves when a new frame has actually been rendered, which
        is the event a capture has to wait for.
        """
        passes = self._ipc.try_command("get_property", "vo-passes")
        if not isinstance(passes, dict) or "fresh" not in passes:
            return None
        return json.dumps(passes["fresh"], sort_keys=True)

    def _wait_for_render(self, marker: str | None, info_before) -> None:
        """Block until the renderer has drawn the frame the player seeked to."""
        deadline = time.monotonic() + RENDER_TIMEOUT

        if marker is not None:
            while time.monotonic() < deadline:
                current = self._render_marker()
                if current != marker:
                    self._marker = current
                    return
                time.sleep(0.004)
            raise SessionError(
                f"mpv's video output drew nothing new within {RENDER_TIMEOUT:.0f}s. Capturing "
                f"now would have produced the previous frame.\n{self.log_tail()}"
            )

        # No vo-passes: not a GPU video output. Wait for the player, then ask
        # for a screenshot and throw it away -- requesting a picture makes the
        # renderer catch up, and the one that comes back after it is right.
        self.best_effort = True
        while time.monotonic() < deadline:
            info = self._frame_info()
            if info is not None and info != info_before:
                break
            time.sleep(0.004)
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="kiyas-mpv-")) / "settle.png"
        self._ipc.try_command(
            "screenshot-to-file", str(self._scratch), "window", timeout=SCREENSHOT_TIMEOUT
        )

    def _seek_to(self, frame: int, fps: Fraction) -> None:
        """Seek so that the video output ends up holding exactly ``frame``.

        The timestamp is ``frame / fps`` and not the middle of the frame's
        display interval. mpv's exact seek lands on the first frame whose
        timestamp is at or after the target, so aiming at the middle of frame
        N's interval lands on N+1 -- measured, on a clip built so that frame N
        has mean luma 2N and the capture identifies itself.
        """
        # The marker from the last render kiyas waited for, not a fresh read:
        # they are the same thing when nothing is outstanding, and when
        # something *is* outstanding a fresh read would already have moved and
        # the barrier would pass on somebody else's frame.
        marker = self._marker if self._marker is not None else self._render_marker()
        info_before = self._frame_info()
        target = max(0.0, frame / float(fps))
        try:
            self._ipc.command("seek", target, "absolute+exact", timeout=SEEK_TIMEOUT)
            self._ipc.wait_event("playback-restart", timeout=SEEK_TIMEOUT)
        except IpcError as exc:
            raise SessionError(f"seeking to frame {frame} failed: {exc}") from exc

        self._wait_for_render(marker, info_before)
        self._current_frame = frame

    def picture_type(self) -> str | None:
        """``I``/``P``/``B`` for the frame currently held, if mpv knows."""
        info = self._frame_info()
        if isinstance(info, dict):
            value = info.get("picture-type")
            return str(value) if value else None
        return None

    def _caption(self, text: str) -> None:
        """Change the burnt-in caption and wait for it to be drawn.

        **The order matters, and getting it wrong is invisible.** Setting the
        caption makes mpv re-render, and that re-render moves the barrier's
        marker. Captioning *before* seeking therefore satisfies the barrier
        with a render of the old picture, and every capture in the run comes
        out one frame behind -- which is precisely what happened on a real 4K
        remux while the same code was passing on a small clip, because there
        the seek finished before the caption did.

        So: seek, wait, caption, wait, capture. The second wait is bounded and
        not an error, because setting the caption to what it already says
        produces no new render at all.
        """
        marker = self._marker
        self._ipc.try_command("set_property", "osd-msg1", text)
        if marker is None:
            return
        deadline = time.monotonic() + CAPTION_TIMEOUT
        while time.monotonic() < deadline:
            current = self._render_marker()
            if current != marker:
                self._marker = current
                return
            time.sleep(0.004)

    def capture(
        self, frame: int, fps: Fraction, destination: Path, *, label: str | None = None
    ) -> Path:
        """Write the rendered frame to ``destination``.

        ``window`` mode, not ``video``: the whole point of driving mpv is to
        capture what its renderer produced -- shaders, tone-mapping curve,
        scaler and all. ``video`` mode would hand back the decoded frame and
        throw away the only thing mpv was here for.

        ``label`` is applied here rather than by the caller so that it cannot
        be applied at the wrong moment; see :meth:`_caption`.
        """
        if frame != self._current_frame:
            self._seek_to(frame, fps)
        if label is not None:
            self._caption(label)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._ipc.command(
                "screenshot-to-file", str(destination), "window", timeout=SCREENSHOT_TIMEOUT
            )
        except IpcError as exc:
            raise SessionError(f"mpv could not write {destination}: {exc}") from exc
        if not destination.is_file():
            raise SessionError(
                f"mpv reported success but {destination} was not written.\n{self.log_tail()}"
            )
        return destination
