"""The tray icon's menu, tooltip and status dot -- and staying alive in it.

The icon itself is :mod:`job_manager.notify`'s, which used to exist only to
carry notifications: right-clicking it did nothing. It now says how many jobs
are running without opening anything, and reaches the job monitor, a new job
or a queue refresh in one click.

"Keep running in the tray" lets MoleditPy's main window be closed while a
long run is still being tracked. MoleditPy quits when its last window closes,
so the option turns that off -- but only while a tray icon exists to quit
from, or a closed window would leave an invisible process behind.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QEvent, QObject, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import notify
from .presence import summary_text, tray_state
from .theme import CY_AMBER, CY_GREEN, CY_RED

_DOT_COLORS = {"busy": CY_GREEN, "queued": CY_AMBER, "error": CY_RED}
_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)

#: How many jobs the menu lists before saying how many more there are.
MENU_JOB_LIMIT = 15


def status_icon(base: QIcon, state: str) -> QIcon:
    """``base`` with a coloured dot in the corner for anything but idle."""
    color = _DOT_COLORS.get(state)
    if color is None or base.isNull():
        return base
    icon = QIcon()
    for size in _ICON_SIZES:
        pixmap = base.pixmap(size, size)
        if pixmap.isNull():
            continue
        pixmap = QPixmap(pixmap)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        diameter = max(6.0, size * 0.42)
        ring = max(1.0, size / 16)
        painter.setPen(QPen(QColor("#ffffff"), ring))
        painter.setBrush(QColor(color))
        offset = size - diameter - ring / 2
        painter.drawEllipse(int(offset), int(offset), int(diameter), int(diameter))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def menu_label(text: str) -> str:
    """A job name as menu text: a bare "&" would underline the next letter."""
    return str(text).replace("&", "&&")


class TrayController(QObject):
    """Owns what the shared tray icon shows and offers."""

    def __init__(self, service, presence, main_window=None, actions: Optional[dict] = None):
        super().__init__()
        self.service = service
        self.presence = presence
        self.main_window = main_window
        #: "monitor", "submit", "host_monitor", "select_job"(job_id)
        self.actions: Dict[str, Callable] = dict(actions or {})
        self.tray: Optional[QSystemTrayIcon] = None
        self.menu: Optional[QMenu] = None
        self._state = ""
        self._icons: Dict[str, QIcon] = {}
        self._saved_quit_on_close: Optional[bool] = None
        self._told_still_running = False
        self._filtering = False

    # --- setup ---------------------------------------------------------------

    def install(self) -> bool:
        self.tray = notify.ensure_tray()
        if self.tray is None:
            return False
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._rebuild_menu)
        self._rebuild_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        if self.main_window is not None:
            try:
                self.main_window.installEventFilter(self)
                self._filtering = True
            except Exception:
                logging.debug("Job Manager: main window not watched", exc_info=True)
        self.apply_keep_running()
        return True

    def _base_icon(self) -> QIcon:
        try:
            from .icon import plugin_icon

            icon = plugin_icon()
            if not icon.isNull():
                return icon
        except Exception:
            logging.debug("Job Manager: no plugin icon for the tray", exc_info=True)
        return notify._icon()

    def update(self, counts: dict, unseen_failures: int) -> None:
        if self.tray is None:
            return
        state = tray_state(counts, unseen_failures)
        try:
            if state != self._state:
                self._state = state
                if state not in self._icons:
                    self._icons[state] = status_icon(self._base_icon(), state)
                self.tray.setIcon(self._icons[state])
            self.tray.setToolTip(self.tooltip(counts, unseen_failures))
        except Exception:
            logging.debug("Job Manager: the tray was not updated", exc_info=True)

    @staticmethod
    def tooltip(counts: dict, unseen_failures: int) -> str:
        lines = ["MoleditPy job manager", summary_text(counts, ", ") or "No active jobs"]
        if unseen_failures:
            lines.append(f"{unseen_failures} failed since you last looked")
        return "\n".join(lines)

    # --- the menu ------------------------------------------------------------

    def _add(self, menu: QMenu, text: str, slot, enabled: bool = True) -> QAction:
        action = menu.addAction(text)
        action.setEnabled(enabled)
        if slot is not None:
            action.triggered.connect(lambda _checked=False: slot())
        return action

    def _rebuild_menu(self) -> None:
        menu = self.menu
        if menu is None:
            return
        menu.clear()
        counts = self.presence.counts() if self.presence is not None else {}
        header = menu.addAction(summary_text(counts, ", ") or "No active jobs")
        header.setEnabled(False)
        menu.addSeparator()
        self._add(menu, "Open Job Monitor", self.actions.get("monitor"))
        self._add(menu, "New Job...", self.actions.get("submit"))
        self._add(menu, "Host Monitor...", self.actions.get("host_monitor"))
        self._add(menu, "Refresh Now", self._refresh_now)

        jobs = self._listed_jobs()
        sub = menu.addMenu(f"Active jobs ({len(jobs)})")
        sub.setEnabled(bool(jobs))
        select = self.actions.get("select_job")
        for job in jobs[:MENU_JOB_LIMIT]:
            label = menu_label(f"{job.name} - {job.state.lower()} on {job.host_name}")
            self._add(sub, label, (lambda job_id=job.id: select(job_id)) if select else None)
        if len(jobs) > MENU_JOB_LIMIT:
            self._add(sub, f"{len(jobs) - MENU_JOB_LIMIT} more...", self.actions.get("monitor"))

        menu.addSeparator()
        self._toggle(menu, "Notify me when a job ends", "notify_on_finish", True)
        self._toggle(menu, "Flash the task bar when a job ends", "flash_on_finish", True)
        keep = self._toggle(
            menu, "Keep running when MoleditPy is closed", "keep_running_in_tray", False
        )
        keep.toggled.connect(lambda _checked: self.apply_keep_running())

        menu.addSeparator()
        if self.main_window is not None and not self._main_visible():
            self._add(menu, "Show MoleditPy", self.show_main_window)
        self._add(menu, "Quit MoleditPy", self.quit_application)

    def _toggle(self, menu: QMenu, text: str, pref: str, default: bool) -> QAction:
        store = self.service.store
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setChecked(bool(store.get_pref(pref, default)))
        action.toggled.connect(lambda checked, key=pref: store.set_pref(key, bool(checked)))
        return action

    def _listed_jobs(self):
        jobs = list(self.service.store.active_jobs())
        # Running first: that is what someone opening this menu is asking about.
        jobs.sort(key=lambda job: (job.state != "RUNNING", job.name.lower()))
        return jobs

    def _refresh_now(self) -> None:
        try:
            if not self.service.poller.refresh_now():
                self.service.message.emit("Refresh is rate limited; try again in a few seconds.")
        except Exception:
            logging.debug("Job Manager: refresh from the tray failed", exc_info=True)

    def _on_activated(self, reason) -> None:
        # macOS opens the menu on a plain click as well; doing both would
        # throw a window up behind the menu the user is reading.
        if sys.platform == "darwin":
            return
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            opener = self.actions.get("monitor")
            if opener is not None:
                opener()

    # --- keeping MoleditPy alive ---------------------------------------------

    def keep_running(self) -> bool:
        return bool(self.service.store.get_pref("keep_running_in_tray", False)) and (
            self.tray is not None
        )

    def apply_keep_running(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        if self.keep_running():
            if self._saved_quit_on_close is None:
                self._saved_quit_on_close = app.quitOnLastWindowClosed()
            app.setQuitOnLastWindowClosed(False)
        elif self._saved_quit_on_close is not None:
            app.setQuitOnLastWindowClosed(self._saved_quit_on_close)
            self._saved_quit_on_close = None
            # Switched off while the main window was already closed: nothing
            # would ever close again to end the process, so bring it back.
            if self.main_window is not None and not self._main_visible():
                self.show_main_window()

    def _main_visible(self) -> bool:
        try:
            return bool(self.main_window.isVisible())
        except RuntimeError:
            return False

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt's spelling
        if watched is self.main_window and event.type() == QEvent.Type.Close:
            # After the host has decided: its own filter may still refuse.
            QTimer.singleShot(0, self._after_main_close)
        return False

    def _after_main_close(self) -> None:
        if self.tray is None or not self.keep_running() or self._main_visible():
            return
        if self._told_still_running:
            return
        self._told_still_running = True
        try:
            self.tray.showMessage(
                "MoleditPy job manager",
                "Still tracking your jobs here. Right-click this icon to reopen or quit.",
                notify._icon(),
                notify.TIMEOUT_MS,
            )
        except Exception:
            logging.debug("Job Manager: the still-running note was refused", exc_info=True)

    def show_main_window(self) -> None:
        window = self.main_window
        if window is None:
            return
        app = QApplication.instance()
        if app is not None:
            # Set by MoleditPy's close handler to hush teardown errors; a
            # window that is back in use must report them again.
            app.setProperty("moleditpy_shutting_down", False)
        try:
            window.show()
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            logging.debug("Job Manager: the main window is gone", exc_info=True)

    def quit_application(self) -> None:
        """Quit MoleditPy -- through its own close first, so unsaved work is asked about."""
        if self.main_window is not None and self._main_visible():
            try:
                if not self.main_window.close():
                    return
            except RuntimeError:
                logging.debug("Job Manager: the main window is gone", exc_info=True)
            if self._main_visible():
                return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # --- teardown --------------------------------------------------------------

    def detach(self) -> None:
        if self._filtering and self.main_window is not None:
            try:
                self.main_window.removeEventFilter(self)
            except RuntimeError:
                pass
            self._filtering = False
        app = QApplication.instance()
        if app is not None and self._saved_quit_on_close is not None:
            app.setQuitOnLastWindowClosed(self._saved_quit_on_close)
            self._saved_quit_on_close = None
            # Without the tray there is no way back to a hidden main window.
            if self.main_window is not None and not self._main_visible():
                self.show_main_window()
        if self.tray is not None:
            try:
                self.tray.activated.disconnect(self._on_activated)
            except (TypeError, RuntimeError):
                pass
            try:
                self.tray.setContextMenu(None)
            except Exception:
                logging.debug("Job Manager: tray menu not removed", exc_info=True)
        self.tray = None
        if self.menu is not None:
            self.menu.deleteLater()
            self.menu = None


__all__ = ["MENU_JOB_LIMIT", "TrayController", "menu_label", "status_icon"]
