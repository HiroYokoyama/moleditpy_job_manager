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


def bring_to_front(window) -> None:
    """Show, restore and raise a window that may be hidden or minimised.

    Restored as well as raised: from the tray a minimised window is the usual
    case, and raise_() alone leaves it on the task bar.
    """
    if window.isMinimized():
        window.showNormal()
    window.show()
    window.raise_()
    window.activateWindow()


#: Modal windows on screen, by name. The tray menu stays usable while one is
#: up, so a second click used to stack a second Settings over the first.
_modal: dict = {}


def exec_once(key: str, build):
    """``build()``'s dialog run modally -- or the one already up, raised.

    Returns the dialog's result, or None when it was only raised.
    """
    existing = _modal.get(key)
    if existing is not None:
        try:
            bring_to_front(existing)
            return None
        except RuntimeError:
            # Deleted under us; fall through and build a fresh one.
            _modal.pop(key, None)
    dialog = build()
    _modal[key] = dialog
    try:
        return dialog.exec()
    finally:
        _modal.pop(key, None)


__all__ = ["bring_to_front", "exec_once", "make_independent"]
