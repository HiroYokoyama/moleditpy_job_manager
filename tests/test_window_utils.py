"""Bringing a window back, and one modal window per name."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager.window_utils import bring_to_front, exec_once  # noqa: E402


class TestBringToFront(unittest.TestCase):
    def test_a_minimised_window_is_restored(self):
        window = MagicMock()
        window.isMinimized.return_value = True
        bring_to_front(window)
        window.showNormal.assert_called_once()
        window.raise_.assert_called_once()

    def test_a_normal_one_is_only_raised(self):
        window = MagicMock()
        window.isMinimized.return_value = False
        bring_to_front(window)
        window.showNormal.assert_not_called()
        window.activateWindow.assert_called_once()


class TestExecOnce(unittest.TestCase):
    def test_a_second_open_raises_the_first(self):
        # The tray menu stays usable under a modal window: a second click used
        # to stack a second Settings over the first.
        built = []

        def build():
            dialog = MagicMock()
            built.append(dialog)
            if len(built) == 1:
                dialog.exec.side_effect = lambda: exec_once("settings", build) or 1
            return dialog

        exec_once("settings", build)
        self.assertEqual(len(built), 1)
        built[0].raise_.assert_called_once()

    def test_once_closed_it_is_built_again(self):
        exec_once("about", MagicMock)
        built = MagicMock()
        exec_once("about", lambda: built)
        built.exec.assert_called_once()

    def test_the_result_is_passed_back(self):
        dialog = MagicMock()
        dialog.exec.return_value = 1
        self.assertEqual(exec_once("api", lambda: dialog), 1)


if __name__ == "__main__":
    unittest.main()
