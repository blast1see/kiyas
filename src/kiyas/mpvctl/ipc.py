"""JSON IPC with a running mpv.

mpv speaks newline-delimited JSON over a Windows named pipe or a Unix socket.
Commands carry a ``request_id`` and get a reply with the same id; events arrive
unsolicited and interleaved with replies.

**Everything here happens on one thread, and that is load-bearing on Windows.**
A handle opened by :func:`open` is a *synchronous* handle, and Windows
serialises I/O on those: while a read is pending, a write from another thread
blocks until the read completes. The obvious design -- a background reader
thread plus commands sent from the caller's thread -- therefore deadlocks after
the first couple of messages. It is not a race; it happens every time, and it
looks exactly like mpv having gone unresponsive. The fix is not a lock. It is
to never have two operations outstanding: ask ``PeekNamedPipe`` how many bytes
are waiting, read exactly that many, and only ever write between reads.

Overlapped I/O would be the other way out, but it needs a hundred lines of
ctypes for no gain: this protocol is strictly request/response, so there is
never anything useful to do while a read is in flight.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

#: How long to wait for mpv to create its pipe or socket before giving up.
CONNECT_TIMEOUT = 20.0

#: Default per-command timeout. Screenshots of a 4K frame take a moment; an
#: indexer-free seek does not.
COMMAND_TIMEOUT = 60.0

_POLL_INTERVAL = 0.004


class IpcError(RuntimeError):
    """Raised when mpv cannot be reached or refuses a command."""


class _WindowsPipe:
    """Client end of an mpv named pipe."""

    def __init__(self, name: str):
        import msvcrt  # noqa: PLC0415 - Windows only

        self._file = open(name, "r+b", buffering=0)  # noqa: SIM115 - closed in close()
        self._handle = msvcrt.get_osfhandle(self._file.fileno())
        self._peek = _peek_named_pipe()

    def available(self) -> int:
        return self._peek(self._handle)

    def read(self, count: int) -> bytes:
        return self._file.read(count) or b""

    def write(self, payload: bytes) -> None:
        self._file.write(payload)

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


def _peek_named_pipe():
    """Return a callable giving the number of bytes waiting on a pipe handle."""
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL

    def peek(handle) -> int:
        waiting = wintypes.DWORD()
        if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(waiting), None):
            raise IpcError("mpv closed the connection")
        return waiting.value

    return peek


class _UnixSocket:
    """Client end of an mpv Unix socket.

    The same single-threaded discipline is kept even though POSIX has no
    handle-serialisation problem, so both platforms run the same code path and
    a bug can only be in one of them.
    """

    def __init__(self, path: str):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(path)
        self._sock.setblocking(False)
        self._pending = b""

    def available(self) -> int:
        if self._pending:
            return len(self._pending)
        try:
            chunk = self._sock.recv(65536)
        except BlockingIOError:
            return 0
        except OSError as exc:
            raise IpcError(f"mpv closed the connection: {exc}") from exc
        if not chunk:
            raise IpcError("mpv closed the connection")
        self._pending = chunk
        return len(chunk)

    def read(self, count: int) -> bytes:
        chunk, self._pending = self._pending[:count], self._pending[count:]
        return chunk

    def write(self, payload: bytes) -> None:
        self._sock.sendall(payload)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def endpoint(tag: str) -> str:
    """A pipe or socket address unique to this process and ``tag``."""
    if os.name == "nt":
        return rf"\\.\pipe\kiyas-{os.getpid()}-{tag}"
    return str(Path(os.environ.get("TMPDIR", "/tmp")) / f"kiyas-{os.getpid()}-{tag}.sock")


class MpvIpc:
    """A connected mpv control channel."""

    def __init__(self, address: str, *, timeout: float = CONNECT_TIMEOUT):
        self._address = address
        self._buffer = b""
        self._events: list[dict[str, Any]] = []
        self._replies: dict[int, dict[str, Any]] = {}
        self._counter = 0
        self._transport = self._connect(address, timeout)

    @staticmethod
    def _connect(address: str, timeout: float):
        # mpv creates the endpoint some way into its startup, so connecting is
        # a retry loop rather than a single attempt.
        deadline = time.monotonic() + timeout
        last: OSError | None = None
        while time.monotonic() < deadline:
            try:
                return _WindowsPipe(address) if os.name == "nt" else _UnixSocket(address)
            except OSError as exc:
                last = exc
                time.sleep(0.05)
        raise IpcError(f"mpv never opened its control channel at {address}: {last}")

    # -- wire ------------------------------------------------------------

    def _pump(self, timeout: float) -> None:
        """Read whatever mpv has already sent, waiting at most ``timeout``."""
        deadline = time.monotonic() + timeout
        while True:
            waiting = self._transport.available()
            if waiting:
                self._buffer += self._transport.read(waiting)
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        # A malformed line is mpv's problem, not a reason to
                        # tear down a working session.
                        continue
                    if "request_id" in message:
                        self._replies[message["request_id"]] = message
                    elif "event" in message:
                        self._events.append(message)
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(_POLL_INTERVAL)

    def command(self, *args: Any, timeout: float = COMMAND_TIMEOUT) -> Any:
        """Send a command and return its ``data``, raising on any mpv error."""
        self._counter += 1
        request_id = self._counter
        payload = json.dumps({"command": list(args), "request_id": request_id})
        try:
            self._transport.write(payload.encode("utf-8") + b"\n")
        except OSError as exc:
            raise IpcError(f"could not send {args[0]!r} to mpv: {exc}") from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self._replies.pop(request_id, None)
            if reply is not None:
                if reply.get("error") != "success":
                    raise IpcError(f"mpv refused {list(args)}: {reply.get('error')}")
                return reply.get("data")
            self._pump(0.05)
        raise IpcError(f"mpv did not answer {list(args)} within {timeout:.0f}s")

    def try_command(self, *args: Any, timeout: float = COMMAND_TIMEOUT) -> Any:
        """Like :meth:`command` but returns ``None`` instead of raising.

        For properties that are legitimately unavailable at times -- the video
        frame before the first one has been rendered, for instance.
        """
        try:
            return self.command(*args, timeout=timeout)
        except IpcError:
            return None

    def wait_event(self, name: str, *, timeout: float = COMMAND_TIMEOUT) -> dict[str, Any]:
        """Wait for a named event, discarding it from the queue."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index, event in enumerate(self._events):
                if event.get("event") == name:
                    return self._events.pop(index)
            self._pump(0.05)
        raise IpcError(f"mpv never reported {name!r} within {timeout:.0f}s")

    def drain_events(self) -> list[dict[str, Any]]:
        """Take everything queued so far. Used by the interactive picker."""
        self._pump(0.0)
        events, self._events = self._events, []
        return events

    def close(self) -> None:
        self._transport.close()
