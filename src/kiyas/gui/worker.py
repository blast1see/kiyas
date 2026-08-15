"""Running the core without freezing the window.

Producing a comparison takes minutes and reports progress the whole time. Doing
that on the UI thread makes the window stop repainting, which Windows then
labels "not responding" -- so the one moment the tool most needs to look like
it is working is the moment it looks broken.

Everything here is one shape: a callable is handed to a thread, its progress
lines come back as a signal, and it ends in either ``done`` or ``failed``. The
work itself knows nothing about Qt, which is what keeps it runnable from the
command line.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal


class Task(QObject):
    """A unit of work, run off the UI thread."""

    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[[Callable[[str], None]], Any]):
        super().__init__()
        self._work = work

    def run(self) -> None:
        try:
            result = self._work(self.progress.emit)
        except Exception as exc:  # noqa: BLE001 - the window reports whatever went wrong
            # Deliberately broad. A traceback in a terminal is a diagnosis; in
            # a window it is a crash, and the useful half is the message.
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.done.emit(result)


class Runner(QObject):
    """Owns the thread a :class:`Task` runs on, and cleans it up after.

    Kept as an attribute of the window rather than a local: a QThread that goes
    out of scope while running is destroyed from under itself, and Qt says so
    by aborting the process.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._task: Task | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(
        self,
        work: Callable[[Callable[[str], None]], Any],
        *,
        on_progress: Callable[[str], None],
        on_done: Callable[[Any], None],
        on_failed: Callable[[str], None],
    ) -> None:
        if self.busy:
            raise RuntimeError("already running")

        thread = QThread()
        task = Task(work)
        task.moveToThread(thread)

        thread.started.connect(task.run)
        task.progress.connect(on_progress)
        task.done.connect(on_done)
        task.failed.connect(on_failed)
        # DirectConnection, so the thread ends itself the moment the work does.
        # The usual spelling is a queued connection, and it only works while
        # the UI event loop is spinning: quit() is a slot on an object that
        # belongs to the *main* thread, so a queued call sits in a queue nobody
        # is reading whenever the main thread is waiting -- which is exactly
        # what closing the window mid-run does. That deadlocked until the wait
        # timed out, five seconds of a frozen window on the way out.
        # QThread.quit is documented thread-safe; calling it from inside the
        # thread's own event loop is just exit(0).
        for signal in (task.done, task.failed):
            signal.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(self._forget)

        self._thread = thread
        self._task = task
        thread.start()

    def _forget(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._task = None

    def wait(self, milliseconds: int = 30000) -> None:
        """Block until the work finishes. For shutdown, and for tests."""
        if self._thread is not None:
            self._thread.wait(milliseconds)
