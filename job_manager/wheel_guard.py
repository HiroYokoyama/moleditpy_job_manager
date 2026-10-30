"""Keep the mouse wheel scrolling a form instead of changing its fields.

Inside a scroll area, a spin box or combo box under the cursor takes the wheel
event, so scrolling stops there and the value changes. With the guard, such a
field reacts to the wheel only once it has focus (click it first); otherwise
the event goes on to the scroll area.
"""

from __future__ import annotations

try:
    from PySide6.QtCore import QEvent, QObject, Qt
    from PySide6.QtWidgets import (
        QAbstractSlider,
        QAbstractSpinBox,
        QComboBox,
        QScrollBar,
        QWidget,
    )
except ImportError:
    # Headless tests replace PySide6 with stand-ins that lack these names.
    QObject = object
    QWidget = None
    _VALUE_WIDGETS: tuple = ()
else:
    _VALUE_WIDGETS = (QAbstractSpinBox, QComboBox, QAbstractSlider)

_GUARDED = "_wheel_guarded"


def _is_value_field(widget) -> bool:
    # A scroll bar is a slider too, but it is what should receive the wheel.
    return isinstance(widget, _VALUE_WIDGETS) and not isinstance(widget, QScrollBar)


class _WheelGuard(QObject):
    def eventFilter(self, obj, event) -> bool:
        etype = event.type()
        if etype == QEvent.Type.Wheel:
            if _is_value_field(obj) and not obj.hasFocus():
                # Ignored and filtered: Qt passes the event on to the parent.
                event.ignore()
                return True
        elif etype == QEvent.Type.ChildPolished:
            # Widgets added later (table rows, rebuilt sections) are covered too.
            child = event.child()
            if isinstance(child, QWidget):
                self.cover(child)
        return False

    def cover(self, widget) -> None:
        if widget.isWindow() or widget.property(_GUARDED):
            return
        widget.setProperty(_GUARDED, True)
        widget.installEventFilter(self)
        if _is_value_field(widget):
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            return
        for child in widget.children():
            if isinstance(child, QWidget):
                self.cover(child)


def install_wheel_guard(root) -> None:
    """Guard every value field under ``root`` (typically a scroll area's widget)."""
    if QWidget is None or not isinstance(root, QWidget):
        # Headless tests build dialogs from stand-in widgets; nothing to guard.
        return
    guard = _WheelGuard(root)
    for child in root.children():
        if isinstance(child, QWidget):
            guard.cover(child)
    root.installEventFilter(guard)
