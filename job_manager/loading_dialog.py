"""A small window saying the host is being waited for, with a way to stop waiting."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from .theme import apply_theme

#: A reply quicker than this needs no window: it would only flash.
SHOW_AFTER_MS = 300


def open_loading_dialog(
    parent: QWidget, title: str, text: str, on_cancel: Callable[[], None]
) -> QProgressDialog:
    """Keep ``parent`` waiting for work of unknown length. Close with close_loading_dialog."""
    dialog = QProgressDialog(text, "Cancel", 0, 0, parent)
    dialog.setWindowTitle(title)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(SHOW_AFTER_MS)
    # What starts the delay: otherwise Qt's own default of four seconds still runs.
    dialog.setValue(0)
    dialog.setMinimumWidth(360)
    apply_theme(dialog)
    dialog.canceled.connect(on_cancel)
    return dialog


def close_loading_dialog(dialog: QProgressDialog) -> None:
    """Close without it counting as a cancel."""
    dialog.canceled.disconnect()
    # reset() stops the timer that would otherwise show it after this.
    dialog.reset()
    dialog.hide()
    dialog.deleteLater()


__all__ = ["SHOW_AFTER_MS", "close_loading_dialog", "open_loading_dialog"]
