"""Shared Qt actions for menus, shortcuts and toolbar buttons."""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QToolBar, QToolButton, QWidget


def make_action(
    parent: QObject,
    text: str,
    callback: Callable,
    tooltip: str = "",
    *,
    shortcut: str = "",
    checked: Optional[bool] = None,
) -> QAction:
    action = QAction(text, parent)
    action.setToolTip(tooltip or text)
    if shortcut:
        action.setShortcut(shortcut)
    if checked is None:
        # triggered carries a bool; open_submit_dialog's first argument is files.
        action.triggered.connect(lambda _checked=False: callback())
    else:
        action.setCheckable(True)
        action.setChecked(checked)
        action.toggled.connect(callback)
    return action


def action_button(action: QAction, parent: Optional[QWidget] = None) -> QToolButton:
    button = QToolButton(parent)
    button.setProperty("jobManagerAction", True)
    button.setDefaultAction(action)
    return button


def action_toolbar(parent: QWidget, actions: Iterable[QAction]) -> QToolBar:
    toolbar = QToolBar(parent)
    toolbar.setMovable(False)
    toolbar.setFloatable(False)
    toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    for action in actions:
        toolbar.addAction(action)
        toolbar.widgetForAction(action).setProperty("jobManagerAction", True)
    return toolbar


def populate_menu(menu: QMenu, actions: Iterable[Optional[QAction]]) -> None:
    for action in actions:
        if action is None:
            menu.addSeparator()
        else:
            menu.addAction(action)
