"""Background work, and its answer reaching a window that may have closed."""

from __future__ import annotations

import unittest

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from job_manager.tasks import gone, run_async  # noqa: E402

_app = QApplication.instance() or QApplication([])


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
        sip.delete(window)

        pool.run()

        self.assertEqual(seen, [])

    def test_an_error_for_a_closed_window_is_dropped_too(self):
        pool = HeldPool()
        window = self.window()
        seen = []

        def fail():
            raise RuntimeError("no route to host")

        run_async(pool, fail, on_error=seen.append, quiet=True, owner=window)
        sip.delete(window)

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


if __name__ == "__main__":
    unittest.main()
