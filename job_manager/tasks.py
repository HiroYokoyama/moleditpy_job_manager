"""One-shot background operations (submit, download, cancel, tail, test).

The poller has its own scheduled task; everything the user triggers by hand
goes through :class:`BackgroundTask`, which runs a plain callable on the shared
thread pool and reports back on the GUI thread.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PyQt6 import sip
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal


class _TaskSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class BackgroundTask(QRunnable):
    """Runs ``fn()`` off the GUI thread. Bind arguments with a lambda."""

    def __init__(self, fn: Callable[[], Any], quiet: bool = False) -> None:
        super().__init__()
        self.fn = fn
        #: Log a failure at debug rather than warning. For work whose failure
        #: is an ordinary outcome the caller already shows -- a host that did
        #: not answer this tick -- where a warning with a traceback would fill
        #: the application log with something nobody has to act on.
        self.quiet = bool(quiet)
        self.signals = _TaskSignals()

    def run(self) -> None:  # pragma: no cover - thread entry; body tested via run_sync
        self.run_sync()

    def run_sync(self) -> Any:
        """Execute inline. Returns the result, or None if it raised."""
        try:
            result = self.fn()
        except Exception as exc:
            if self.quiet:
                logging.debug("Job Manager: background task failed: %s", exc, exc_info=True)
            else:
                logging.warning("Job Manager: background task failed: %s", exc, exc_info=True)
            self.signals.failed.emit(str(exc))
            self.signals.finished.emit()
            return None
        self.signals.succeeded.emit(result)
        self.signals.finished.emit()
        return result


def gone(owner: Optional[QObject]) -> bool:
    """Whether ``owner``'s C++ object has been destroyed."""
    if owner is None:
        return False
    try:
        return sip.isdeleted(owner)
    except TypeError:
        # Not a sip wrapper at all (a test double): nothing to outlive.
        return False


def _guarded(owner: QObject, callback: Optional[Callable]) -> Optional[Callable]:
    if callback is None:
        return None

    def call(*args):
        if gone(owner):
            return None
        return callback(*args)

    return call


def run_async(
    pool: QThreadPool,
    fn: Callable[[], Any],
    on_success: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
    on_finished: Optional[Callable[[], None]] = None,
    *,
    quiet: bool = False,
    owner: Optional[QObject] = None,
) -> BackgroundTask:
    """Queue ``fn`` and wire its callbacks. Returns the task (kept by the pool).

    ``quiet`` is for work whose failure the caller reports itself: it is logged
    at debug rather than warning, so an unreachable host does not write a
    traceback into the application log every time it is asked.

    ``owner`` is the window the callbacks draw into. They are closures, not
    slots, so Qt does not disconnect them when it is destroyed: a dialog closed
    while its work was in flight had the answer delivered into widgets that no
    longer existed, which raised, or crashed the process outright. With an
    owner, a callback arriving after it is gone is dropped.
    """
    if owner is not None:
        on_success = _guarded(owner, on_success)
        on_error = _guarded(owner, on_error)
        on_finished = _guarded(owner, on_finished)
    task = BackgroundTask(fn, quiet=quiet)
    if on_success is not None:
        task.signals.succeeded.connect(on_success)
    if on_error is not None:
        task.signals.failed.connect(on_error)
    if on_finished is not None:
        task.signals.finished.connect(on_finished)
    pool.start(task)
    return task


__all__ = ["BackgroundTask", "run_async"]
