"""A drop never tells the file manager that the file was moved here.

Shift held during a drag from Explorer proposes a move. Accepting that tells
Explorer the file now lives here, and it removes the original.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import QCoreApplication, QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402

from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .test_dialogs import DialogTestCase  # noqa: E402

COPY_OR_MOVE = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
SHIFT = Qt.KeyboardModifier.ShiftModifier


class TestDropsAreCopies(DialogTestCase):
    def setUp(self):
        super().setUp()
        #: The events do not own their mime data; it has to outlive them.
        self.mime = QMimeData()

    def drop(self, *paths, actions=COPY_OR_MOVE) -> QDropEvent:
        self.mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        return QDropEvent(QPointF(1, 1), actions, self.mime, Qt.MouseButton.LeftButton, SHIFT)

    def drag(self, *paths) -> QDragEnterEvent:
        self.mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        return QDragEnterEvent(
            QPoint(1, 1), COPY_OR_MOVE, self.mime, Qt.MouseButton.LeftButton, SHIFT
        )

    def windows(self):
        monitor = JobsDialog(self.service)
        self.addCleanup(monitor.deleteLater)
        wizard = SubmitDialog(self.service)
        self.addCleanup(wizard.deleteLater)
        return monitor, wizard

    def test_a_shift_drag_really_does_propose_a_move(self):
        # Without this the tests below would pass for the wrong reason.
        event = self.drop(self.make_input("a.inp"))
        self.assertEqual(event.proposedAction(), Qt.DropAction.MoveAction)

    def test_dragging_over_shows_and_accepts_a_copy(self):
        path = self.make_input("a.inp")
        for window in self.windows():
            with self.subTest(window=type(window).__name__):
                event = self.drag(path)
                window.dragEnterEvent(event)
                self.assertTrue(event.isAccepted())
                self.assertEqual(event.dropAction(), Qt.DropAction.CopyAction)

    def test_dropping_an_input_is_a_copy(self):
        path = self.make_input("a.inp")
        for window in self.windows():
            with self.subTest(window=type(window).__name__):
                event = self.drop(path)
                with patch.object(window, "open_submit_dialog", create=True):
                    window.dropEvent(event)
                    # The window opens after the drop returns: settle it here.
                    QCoreApplication.processEvents()
                self.assertTrue(event.isAccepted())
                self.assertEqual(event.dropAction(), Qt.DropAction.CopyAction)

    def test_dropping_a_job_list_is_a_copy(self):
        monitor, _wizard = self.windows()
        event = self.drop(self.store.export_jobs(self.make_input("list.pmejbs")))
        with patch.object(monitor, "open_job_list"):
            monitor.dropEvent(event)
            QCoreApplication.processEvents()
        self.assertEqual(event.dropAction(), Qt.DropAction.CopyAction)

    def test_a_source_that_can_only_move_is_refused(self):
        path = self.make_input("a.inp")
        for window in self.windows():
            with self.subTest(window=type(window).__name__):
                event = self.drop(path, actions=Qt.DropAction.MoveAction)
                with patch.object(window, "open_submit_dialog", create=True) as opened:
                    window.dropEvent(event)
                self.assertFalse(event.isAccepted())
                opened.assert_not_called()
