"""A desktop notification when a job ends, via ``QSystemTrayIcon.showMessage``.

The tray icon is created lazily on first notification (never at import) and
removed on shutdown, since a leftover tray icon outlives the plugin. Everything
is guarded: no tray, headless, or a refused call are normal, not errors.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtWidgets import QApplication, QStyle, QSystemTrayIcon

_tray: Optional[QSystemTrayIcon] = None

#: How long the message stays up, where the platform honours a duration at all.
TIMEOUT_MS = 8000


def available() -> bool:
    """Whether this desktop can show a notification at all."""
    try:
        return bool(QApplication.instance()) and QSystemTrayIcon.isSystemTrayAvailable()
    except Exception:
        logging.debug("Job Manager: no system tray", exc_info=True)
        return False


def _icon():
    """The application's own icon, falling back to a stock one.

    A null icon is invisible or drops the message on some platforms.
    """
    app = QApplication.instance()
    icon = app.windowIcon() if app is not None else None
    if icon is not None and not icon.isNull():
        return icon
    return app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)


def notify(title: str, message: str) -> bool:
    """Raise a desktop notification. False when the platform has none."""
    global _tray
    if not available():
        return False
    try:
        if _tray is None:
            _tray = QSystemTrayIcon(_icon())
            _tray.setToolTip("MoleditPy job manager")
            _tray.show()
        _tray.showMessage(title, message, _icon(), TIMEOUT_MS)
    except Exception:
        logging.debug("Job Manager: the notification was refused", exc_info=True)
        return False
    return True


def shutdown() -> None:
    """Take the tray icon away. Called when the plugin stops tracking."""
    global _tray
    if _tray is None:
        return
    try:
        _tray.hide()
        _tray.setParent(None)
    except Exception:
        logging.debug("Job Manager: the tray icon was not removed", exc_info=True)
    _tray = None


__all__ = ["TIMEOUT_MS", "available", "notify", "shutdown"]
