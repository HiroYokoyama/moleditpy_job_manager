"""The wheel guard's decisions, against small Qt stand-ins.

The guard is loaded from its file under fake PySide6 modules, so this runs
where PySide6 is absent too. What Qt itself does with an ignored wheel event is
not modelled here; this checks which widgets are guarded and what the filter
returns for each event.
"""

from __future__ import annotations

import importlib.util
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest import mock

PACKAGE = "job_manager"
_SOURCE = Path(__file__).resolve().parent.parent / PACKAGE / "wheel_guard.py"


class _Type(Enum):
    Wheel = 1
    ChildPolished = 2
    Paint = 3


class FakeEvent:
    Type = _Type

    def __init__(self, etype, child=None):
        self._type = etype
        self._child = child
        self.accepted = True

    def type(self):
        return self._type

    def child(self):
        return self._child

    def ignore(self):
        self.accepted = False


class FakeObject:
    def __init__(self, parent=None):
        self._children = []
        self.filters = []
        if parent is not None:
            parent._children.append(self)

    def children(self):
        return list(self._children)

    def installEventFilter(self, guard):
        self.filters.append(guard)


class FakeWidget(FakeObject):
    def __init__(self, parent=None, window=False, focused=False):
        super().__init__(parent)
        self._window = window
        self._focused = focused
        self._props = {}
        self.focus_policy = "wheel"

    def isWindow(self):
        return self._window

    def hasFocus(self):
        return self._focused

    def property(self, name):
        return self._props.get(name)

    def setProperty(self, name, value):
        self._props[name] = value

    def setFocusPolicy(self, policy):
        self.focus_policy = policy


class FakeSpinBox(FakeWidget):
    pass


class FakeComboBox(FakeWidget):
    pass


class FakeSlider(FakeWidget):
    pass


class FakeScrollBar(FakeSlider):
    pass


def _load(with_names=True):
    core = types.ModuleType("PySide6.QtCore")
    widgets = types.ModuleType("PySide6.QtWidgets")
    if with_names:
        core.QEvent = FakeEvent
        core.QObject = FakeObject
        core.Qt = types.SimpleNamespace(FocusPolicy=types.SimpleNamespace(StrongFocus="strong"))
        widgets.QAbstractSlider = FakeSlider
        widgets.QAbstractSpinBox = FakeSpinBox
        widgets.QComboBox = FakeComboBox
        widgets.QScrollBar = FakeScrollBar
        widgets.QWidget = FakeWidget
    pyside = types.ModuleType("PySide6")
    pyside.QtCore = core
    pyside.QtWidgets = widgets
    fakes = {"PySide6": pyside, "PySide6.QtCore": core, "PySide6.QtWidgets": widgets}
    with mock.patch.dict("sys.modules", fakes):
        spec = importlib.util.spec_from_file_location("_wheel_guard_under_test", _SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class TestWithoutQt(unittest.TestCase):
    def test_stand_ins_without_the_names_disable_the_guard(self):
        module = _load(with_names=False)
        root = FakeWidget()
        module.install_wheel_guard(root)
        self.assertEqual(root.filters, [])


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.module = _load()
        self.root = FakeWidget()
        self.group = FakeWidget(self.root)
        self.spin = FakeSpinBox(self.group)
        self.combo = FakeComboBox(self.root)
        self.bar = FakeScrollBar(self.root)
        self.popup = FakeWidget(self.root, window=True)
        self.inside_popup = FakeSpinBox(self.popup)
        FakeObject(self.root)  # a non-widget child, such as a layout
        self.module.install_wheel_guard(self.root)
        self.guard = self.root.filters[0]


class TestWhatIsGuarded(GuardTestCase):
    def test_a_non_widget_root_is_left_alone(self):
        self.module.install_wheel_guard(object())

    def test_value_fields_are_filtered_and_lose_wheel_focus(self):
        for field in (self.spin, self.combo):
            self.assertEqual(field.filters, [self.guard])
            self.assertEqual(field.focus_policy, "strong")

    def test_containers_are_filtered_to_see_new_children(self):
        self.assertEqual(self.group.filters, [self.guard])

    def test_scroll_bars_keep_their_focus_policy(self):
        self.assertEqual(self.bar.focus_policy, "wheel")

    def test_separate_windows_are_skipped(self):
        self.assertEqual(self.popup.filters, [])
        self.assertEqual(self.inside_popup.filters, [])

    def test_covering_twice_installs_one_filter(self):
        self.guard.cover(self.spin)
        self.assertEqual(self.spin.filters, [self.guard])


class TestTheFilter(GuardTestCase):
    def _wheel(self, widget):
        event = FakeEvent(_Type.Wheel)
        return self.guard.eventFilter(widget, event), event.accepted

    def test_an_unfocused_field_passes_the_wheel_on(self):
        self.assertEqual(self._wheel(self.spin), (True, False))

    def test_a_focused_field_keeps_the_wheel(self):
        self.spin._focused = True
        self.assertEqual(self._wheel(self.spin), (False, True))

    def test_scroll_bars_and_containers_keep_the_wheel(self):
        self.assertEqual(self._wheel(self.bar), (False, True))
        self.assertEqual(self._wheel(self.group), (False, True))

    def test_a_child_polished_later_is_covered(self):
        late = FakeComboBox(self.group)
        self.guard.eventFilter(self.group, FakeEvent(_Type.ChildPolished, late))
        self.assertEqual(late.filters, [self.guard])

    def test_a_non_widget_child_is_ignored(self):
        self.assertFalse(
            self.guard.eventFilter(self.group, FakeEvent(_Type.ChildPolished, object()))
        )

    def test_other_events_pass_through(self):
        self.assertFalse(self.guard.eventFilter(self.spin, FakeEvent(_Type.Paint)))
