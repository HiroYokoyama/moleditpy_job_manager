"""One-shot background operations (submit, download, cancel, tail, test).

The poller has its own scheduled task; everything the user triggers by hand
goes through :class:`BackgroundTask`, which runs a plain callable on the shared
thread pool and reports back on the GUI thread.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal


class _TaskSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class BackgroundTask(QRunnable):
    """Runs ``fn(*args, **kwargs)`` off the GUI thread."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        #: Log a failure at debug rather than warning. For work whose failure
        #: is an ordinary outcome the caller already shows -- a host that did
        #: not answer this tick -- where a warning with a traceback would fill
        #: the application log with something nobody has to act on.
        self.quiet = bool(kwargs.pop("_quiet", False))
        self.signals = _TaskSignals()

    def run(self) -> None:  # pragma: no cover - thread entry; body tested via run_sync
        self.run_sync()

    def run_sync(self) -> Any:
        """Execute inline. Returns the result, or None if it raised."""
        try:
            result = self.fn(*self.args, **self.kwargs)
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


def run_async(
    pool: QThreadPool,
    fn: Callable[..., Any],
    on_success: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
    on_finished: Optional[Callable[[], None]] = None,
    *args: Any,
    quiet: bool = False,
    **kwargs: Any,
) -> BackgroundTask:
    """Queue ``fn`` and wire its callbacks. Returns the task (kept by the pool).

    ``quiet`` is for work whose failure the caller reports itself: it is logged
    at debug rather than warning, so an unreachable host does not write a
    traceback into the application log every time it is asked.
    """
    task = BackgroundTask(fn, *args, _quiet=quiet, **kwargs)
    if on_success is not None:
        task.signals.succeeded.connect(on_success)
    if on_error is not None:
        task.signals.failed.connect(on_error)
    if on_finished is not None:
        task.signals.finished.connect(on_finished)
    pool.start(task)
    return task


__all__ = ["BackgroundTask", "run_async"]
