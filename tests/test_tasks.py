"""Background work, and its answer reaching a window that may have closed."""

from __future__ import annotations

import unittest
import threading
import weakref
import gc

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent, QThreadPool, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from job_manager.tasks import BackgroundTask, gone, run_async  # noqa: E402
from job_manager.poller import _PollTask  # noqa: E402
from job_manager.transport.base import TransportError  # noqa: E402
from .fakes import FakeTransport, make_host  # noqa: E402

_app = QApplication.instance() or QApplication([])


@pytest.mark.parametrize("kind", ["background", "poll"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("cycle", [False, True])
def test_pool_senders_are_destroyed_on_gui_thread(kind, fails, cycle):
    """Finishing a runnable must not delete its QObject on the pool thread."""
    host = make_host()

    def work(*args):
        if fails:
            raise TransportError("host unavailable")
        return FakeTransport(host) if args else "done"

    task = BackgroundTask(work, quiet=True) if kind == "background" else _PollTask(host, [], work)
    if cycle:
        task.retained = task
    destroyed = []
    task.signals.destroyed.connect(
        lambda: destroyed.append(threading.get_ident()), Qt.ConnectionType.DirectConnection
    )
    sender = weakref.ref(task.signals)
    pool = QThreadPool()
    pool.start(task)
    del task
    assert pool.waitForDone(5000)
    assert destroyed == []
    assert sender() is not None and shiboken6.isValid(sender())
    # Qt finishing the runnable does not collect cycles in its Python wrapper.
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed == [threading.get_ident()]


def test_clearing_queued_task_also_defers_sender_cleanup():
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    gate = threading.Event()
    entered = threading.Event()

    def block():
        entered.set()
        gate.wait(5)

    pool.start(BackgroundTask(block))
    try:
        assert entered.wait(5)
        task = BackgroundTask(lambda: pytest.fail("cleared work must not run"))
        sender = weakref.ref(task.signals)
        pool.start(task)
        del task
        pool.clear()
        assert sender() is not None and shiboken6.isValid(sender())
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert sender() is None or not shiboken6.isValid(sender())
    finally:
        gate.set()
        assert pool.waitForDone(5000)


class HeldPool:
    """Keeps the task until the test runs it: work in flight while a window closes."""

    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def run(self):
        for task in self.tasks:
            task.run_sync()


class TestAnOwnerThatHasGone(unittest.TestCase):
    def window(self):
        window = QWidget()
        window.label = QLabel(window)
        return window

    def test_an_answer_for_a_closed_window_is_dropped(self):
        pool = HeldPool()
        window = self.window()
        seen = []
        run_async(
            pool,
            lambda: "done",
            on_success=lambda text: seen.append(window.label.setText(text)),
            owner=window,
        )
        shiboken6.delete(window)

        pool.run()

        self.assertEqual(seen, [])

    def test_an_error_for_a_closed_window_is_dropped_too(self):
        pool = HeldPool()
        window = self.window()
        seen = []

        def fail():
            raise RuntimeError("no route to host")

        run_async(pool, fail, on_error=seen.append, quiet=True, owner=window)
        shiboken6.delete(window)

        pool.run()

        self.assertEqual(seen, [])

    def test_an_open_window_still_gets_its_answer(self):
        pool = HeldPool()
        window = self.window()
        self.addCleanup(window.deleteLater)
        run_async(pool, lambda: "done", on_success=window.label.setText, owner=window)

        pool.run()

        self.assertEqual(window.label.text(), "done")

    def test_without_an_owner_nothing_changes(self):
        pool = HeldPool()
        seen = []
        run_async(pool, lambda: 1, on_success=seen.append, on_finished=lambda: seen.append("end"))
        pool.run()
        self.assertEqual(seen, [1, "end"])

    def test_something_that_is_not_qt_is_never_gone(self):
        self.assertFalse(gone(None))
        self.assertFalse(gone(object()))

    def test_worker_answers_and_cleanup_run_in_the_owners_thread(self):
        pool = HeldPool()
        window = self.window()
        seen = []
        main_thread = threading.get_ident()
        run_async(
            pool,
            lambda: "done",
            on_success=lambda value: seen.append((value, threading.get_ident())),
            on_finished=lambda: seen.append(("finished", threading.get_ident())),
            owner=window,
        )
        worker = threading.Thread(target=pool.run)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(seen, [])
        _app.processEvents()
        self.assertEqual(seen, [("done", main_thread), ("finished", main_thread)])
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(window.findChildren(type(window.label)), [window.label])
        self.assertEqual(len(window.children()), 1)
        shiboken6.delete(window)

    def test_destroying_owner_drops_already_queued_worker_answers(self):
        pool = HeldPool()
        window = self.window()
        seen = []
        run_async(pool, lambda: "done", on_success=seen.append, owner=window)
        worker = threading.Thread(target=pool.run)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        shiboken6.delete(window)
        _app.processEvents()
        self.assertEqual(seen, [])

    def test_success_callback_can_destroy_its_owner(self):
        pool = HeldPool()
        window = self.window()
        seen = []
        run_async(
            pool,
            lambda: "done",
            on_success=lambda value: shiboken6.delete(window),
            on_finished=lambda: seen.append("finished"),
            owner=window,
        )
        pool.run()
        _app.processEvents()
        self.assertTrue(not shiboken6.isValid(window))
        self.assertEqual(seen, [])


class TestARealPoolWithNoOwner(unittest.TestCase):
    """The pool deletes each finished task on its own thread, signals and all."""

    def test_every_answer_arrives(self):
        pool = QThreadPool()
        pool.setMaxThreadCount(2)
        count = 300
        values, finished = [], []
        for i in range(count):
            run_async(
                pool,
                lambda i=i: i,
                on_success=values.append,
                on_finished=lambda: finished.append(1),
            )
        pool.waitForDone()
        clock = QElapsedTimer()
        clock.start()
        while len(finished) < count and clock.elapsed() < 10000:
            _app.processEvents()
        self.assertEqual(sorted(values), list(range(count)))
        self.assertEqual(len(finished), count)

    def test_receivers_are_cleaned_up(self):
        pool = QThreadPool()
        run_async(pool, lambda: 1)
        pool.waitForDone()
        _app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(pool.children(), [])


if __name__ == "__main__":
    unittest.main()
