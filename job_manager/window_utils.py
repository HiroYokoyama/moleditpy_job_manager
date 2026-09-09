"""Make a plugin dialog behave like a window rather than a dialog.

A QDialog with a parent is tied to it: minimising MoleditPy minimises the
monitor with it, it gets no task bar entry of its own, and on most platforms it
cannot be maximised. None of that suits a window somebody keeps open beside the
application for hours, watching a queue.

The parent is still passed, because it is what owns the window and destroys it
with the plugin; only the behaviour changes.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt


def make_independent(dialog) -> None:
    """Own task bar entry, own minimise and maximise, own life on screen."""
    dialog.setParent(None)
    dialog.setWindowFlags(
        Qt.WindowType.Window
        | Qt.WindowType.WindowMinimizeButtonHint
        | Qt.WindowType.WindowMaximizeButtonHint
        | Qt.WindowType.WindowCloseButtonHint
    )
    dialog.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
    # Here rather than in each window: a window with its own task bar entry
    # needs its own icon, or it shows the generic one while every other
    # MoleditPy window shows the application's. Imported inside the function
    # so the icon's dependencies are not pulled in by every dialog module.
    from .icon import apply_icon

    apply_icon(dialog)


__all__ = ["make_independent"]
