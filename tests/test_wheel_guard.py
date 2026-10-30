"""The wheel scrolls a form; it changes a field only once that field has focus."""

from __future__ import annotations

import unittest

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from job_manager.wheel_guard import install_wheel_guard  # noqa: E402


def _wheel(widget, dy=-120):
    """Send one notch down; return whether the widget kept the event.

    A synthetic event is not propagated by Qt, so the check is the one Qt makes
    for a real one: an event left unaccepted goes on to the parent.
    """
    event = QWheelEvent(
        QPointF(5, 5),
        QPointF(widget.mapToGlobal(QPoint(5, 5))),
        QPoint(),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)
    return event.isAccepted()


class WheelGuardTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.scroll = QScrollArea()
        self.addCleanup(self.scroll.deleteLater)
        self.scroll.setWidgetResizable(True)
        self.scroll.resize(300, 200)
        body = QWidget()
        self.layout = QVBoxLayout(body)
        self.scroll.setWidget(body)
        install_wheel_guard(body)
        self.spin = QSpinBox()
        self.spin.setRange(0, 100)
        self.spin.setValue(50)
        self.layout.addWidget(self.spin)
        self.table = QTableWidget(2, 1)
        self.layout.addWidget(self.table)
        for _ in range(20):
            self.layout.addWidget(QSpinBox())
        self.scroll.show()
        self.app.processEvents()
        self.spin.clearFocus()


class TestWheelGuard(WheelGuardTestCase):
    def test_an_unfocused_field_passes_the_wheel_on(self):
        self.assertFalse(_wheel(self.spin))
        self.assertEqual(self.spin.value(), 50)

    def test_a_focused_field_still_takes_the_wheel(self):
        self.spin.setFocus()
        self.assertTrue(_wheel(self.spin))
        self.assertEqual(self.spin.value(), 49)

    def test_the_wheel_no_longer_gives_a_field_focus(self):
        self.assertEqual(self.spin.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_fields_added_after_show_are_guarded_too(self):
        late = QComboBox()
        late.addItems(["a", "b", "c"])
        late.setCurrentIndex(1)
        self.layout.addWidget(late)
        cell = QComboBox()
        cell.addItems(["a", "b"])
        self.table.setCellWidget(0, 0, cell)
        self.app.processEvents()
        self.assertFalse(_wheel(late))
        self.assertEqual(late.currentIndex(), 1)
        self.assertFalse(_wheel(cell))
        self.assertEqual(cell.currentIndex(), 0)

    def test_scroll_bars_keep_the_wheel(self):
        bar = self.scroll.verticalScrollBar()
        self.assertTrue(_wheel(bar))
        self.assertGreater(bar.value(), 0)

    def test_a_stand_in_widget_is_left_alone(self):
        install_wheel_guard(object())
