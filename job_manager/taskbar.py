"""The number on the application's icon in the OS task bar or Dock.

``QGuiApplication.setBadgeNumber`` is the one call that covers every desktop
platform MoleditPy runs on: the Dock icon on macOS, the task bar button on
Windows, and the launcher entry on Linux desktops that implement the Unity
launcher protocol. There is no Qt 6 equivalent of Qt 5's QtWinExtras taskbar
progress, and reaching for ``ITaskbarList3`` by hand would buy a progress bar
on one platform in exchange for COM plumbing on all of them.

Qt added it in 6.5. MoleditPy's own dependency floor is lower than that, so
every call here is guarded: an older Qt simply has no badge, and the status bar
counter carries the same information anyway.
"""

from __future__ import annotations

import logging

from PyQt6.QtGui import QGuiApplication

#: False on Qt < 6.5, where the badge does not exist.
SUPPORTED = hasattr(QGuiApplication, "setBadgeNumber")


def set_badge(count: int) -> bool:
    """Show ``count`` on the application icon; 0 clears it. True if applied.

    Returns False rather than raising when the platform has no badge, which is
    the normal case on a bare window manager and on Qt < 6.5.
    """
    if not SUPPORTED:
        return False
    try:
        QGuiApplication.setBadgeNumber(max(0, int(count)))
    except Exception:
        # A platform plugin that does not implement it is not an error worth
        # showing anyone; the status bar counter says the same thing.
        logging.debug("Job Manager: the task bar badge was refused", exc_info=True)
        return False
    return True


def clear_badge() -> bool:
    """Take the badge off the icon. Called when the plugin stops tracking.

    A badge left behind outlives the thing it described: the plugin unloading,
    or the last job finishing, must not leave MoleditPy's icon claiming three
    jobs are running for the rest of the session.
    """
    return set_badge(0)
