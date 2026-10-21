"""One-shot background operations (submit, download, cancel, tail, test).

The poller has its own scheduled task; everything the user triggers by hand
goes through :class:`BackgroundTask`, which runs a plain callable on the shared
thread pool and reports back on the GUI thread.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any, Callable, Optional

from shiboken6 import isValid
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRunnable, QThreadPool, Signal, Slot


class _TaskSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class _SignalRunnable(QRunnable):
    """Keep a worker's GUI-affine sender alive until deferred GUI cleanup."""

    def __init__(self, signal_type) -> None:
        super().__init__()
        # QRunnable auto-deletion releases Python attributes on the pool thread.
        # A Qt parent keeps the sender alive after that last Python reference;
        # deleteLater then destroys it on its own thread, also for cleared tasks.
        self.signals = signal_type(QCoreApplication.instance())
        self._started = False

    def __del__(self):
        signals = getattr(self, "signals", None)
        if signals is not None and isValid(signals):
            signals.deleteLater()


class BackgroundTask(_SignalRunnable):
    """Runs ``fn()`` off the GUI thread. Bind arguments with a lambda."""

    def __init__(self, fn: Callable[[], Any], quiet: bool = False) -> None:
        super().__init__(_TaskSignals)
        self.fn = fn
        #: Log a failure at debug rather than warning. For work whose failure
        #: is an ordinary outcome the caller already shows -- a host that did
        #: not answer this tick -- where a warning with a traceback would fill
        #: the application log with something nobody has to act on.
        self.quiet = bool(quiet)

    def run(self) -> None:  # pragma: no cover - thread entry; body tested via run_sync
        self.run_sync()

    def run_sync(self) -> Any:
        """Execute inline. Returns the result, or None if it raised."""
        self._started = True
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
        return not isValid(owner)
    except TypeError:
        # Not a Qt wrapper at all (a test double): nothing to outlive.
        return False


def _guarded(owner: QObject, callback: Optional[Callable]) -> Optional[Callable]:
    if callback is None:
        return None

    def call(*args):
        if gone(owner):
            return None
        return callback(*args)

    return call


class _CallbackReceiver(QObject):
    """Deliver a task's answer in the owner's thread, for the owner's lifetime.

    Every callback goes through one of these, owner or not. Previously the
    pool deleted a task and its parentless sender on its own thread, taking
    along PySide's hidden closure receiver while GUI calls were still queued.
    A QObject slot keeps delivery independent of that temporary receiver;
    _SignalRunnable separately keeps sender destruction on the GUI thread.
    """

    def __init__(self, owner, on_success, on_error, on_finished):
        super().__init__(owner)
        self.on_success = on_success
        self.on_error = on_error
        self.on_finished = on_finished
        self.cancelled = False

    @Slot(object)
    def succeeded(self, value):
        if not self.cancelled and self.on_success is not None:
            self.on_success(value)

    @Slot(str)
    def failed(self, error):
        if not self.cancelled and self.on_error is not None:
            self.on_error(error)

    @Slot()
    def finished(self):
        try:
            if not self.cancelled and self.on_finished is not None:
                self.on_finished()
        finally:
            # A callback may itself close and destroy the owning window.
            if not gone(self):
                self.deleteLater()

    def discard(self):
        self.cancelled = True
        self.deleteLater()
        self.on_success = self.on_error = self.on_finished = None


_receivers: weakref.WeakSet = weakref.WeakSet()


def shutdown_pool(pool, after_clear=None) -> None:
    """Drain workers while GUI references remain pinned, then retire callbacks."""

    def pending():
        return [
            (receiver, receiver.parent(), receiver.task())
            for receiver in list(_receivers)
            if not gone(receiver) and receiver.pool_id == id(pool)
        ]

    # Keep queued tasks and their UI owners alive on this thread even when
    # clear() deletes their C++ runnable, or a worker drops its last reference.
    held = pending()
    pool.clear()
    if after_clear is not None:
        after_clear()
    known = {id(receiver) for receiver, _, _ in held}
    held.extend(record for record in pending() if id(record[0]) not in known)
    pool._job_manager_stopping = True
    for receiver, _, _ in held:
        receiver.cancelled = not receiver.deliver_on_shutdown
    pool.waitForDone()
    for receiver, _, task in held:
        if gone(receiver):
            continue
        if receiver.deliver_on_shutdown:
            # Persist completed submissions/downloads before retiring the
            # service. UI callbacks are dropped instead of reviving dialogs.
            if task is not None and not task._started:
                receiver.failed("Operation cancelled because Job Manager is shutting down")
            else:
                QCoreApplication.sendPostedEvents(receiver, QEvent.Type.MetaCall)
        if not gone(receiver):
            receiver.discard()


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
    owner, a QObject child receives the signals in the owner's thread, and Qt
    disconnects it automatically when the owner is destroyed. Without one the
    receiver is a child of the pool (or the application), never a bare
    closure; see :class:`_CallbackReceiver`.
    """
    if getattr(pool, "_job_manager_stopping", False):
        raise RuntimeError("Job Manager is shutting down")
    task = BackgroundTask(fn, quiet=quiet)
    if isinstance(owner, QObject):
        if gone(owner):
            pool.start(task)
            return task
        parent = owner
    else:
        if owner is not None:
            on_success = _guarded(owner, on_success)
            on_error = _guarded(owner, on_error)
            on_finished = _guarded(owner, on_finished)
        parent = pool if isinstance(pool, QObject) else QCoreApplication.instance()
    receiver = _CallbackReceiver(parent, on_success, on_error, on_finished)
    receiver.pool_id = id(pool)
    receiver.task = weakref.ref(task)
    receiver.deliver_on_shutdown = owner is None
    _receivers.add(receiver)
    if parent is None:
        _keep_unparented(receiver)
    task.signals.succeeded.connect(receiver.succeeded)
    task.signals.failed.connect(receiver.failed)
    task.signals.finished.connect(receiver.finished)
    pool.start(task)
    return task


#: Receivers with no QObject to own them (no application object yet). Pruned
#: here once ``deleteLater`` has destroyed them, never from inside their own
#: slot: freeing a wrapper while its slot is still running crashes in shiboken.
_unparented: set = set()


def _keep_unparented(receiver: _CallbackReceiver) -> None:
    for old in [r for r in _unparented if gone(r)]:
        _unparented.discard(old)
    _unparented.add(receiver)


__all__ = ["BackgroundTask", "run_async", "shutdown_pool"]
