"""A desktop notification when a job ends.

The badge and the status bar counter both answer "how many are running", which
is a number you have to go and look at. For a calculation that runs for six
hours the useful event is the *transition* -- and by then MoleditPy is usually
behind something else, or minimised.

``QSystemTrayIcon.showMessage`` is the portable way to raise one: Notification
Center on macOS, the action centre on Windows, and whatever the desktop
implements on Linux. It needs a tray icon to hang the message on, so one is
created lazily -- on first notification, never at import -- and removed again on
shutdown, since a tray icon left behind outlives the plugin that put it there.

Everything is guarded. A headless session, a desktop with no tray, and a
platform plugin that refuses the call are all normal outcomes, not errors: the
job is still tracked and the monitor still says so.
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

    A tray icon with a null icon is invisible on some platforms and silently
    drops the message on others, so there has to be something.
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
            # Named so a user looking at a tray full of icons can tell whose
            # this is before clicking it.
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
