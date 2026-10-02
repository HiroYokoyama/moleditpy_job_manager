"""The tray process MoleditPy hands its jobs to when it closes.

Started by :mod:`job_manager.handoff` as ``__main__.py --tray``. No window opens
on its own: the tray icon is the whole interface until something is clicked,
and the job monitor it opens is the same one the plugin shows. Results are
still downloaded, but not opened -- there is no MoleditPy to open them in.

It ends when asked from its menu, or when a MoleditPy starting up asks it to
through the stop file, so the two never track the same jobs at once.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QApplication

from . import handoff, notify, presence


class StandaloneTray(QObject):
    """Owns the tray process: its heartbeat, its windows, and when it ends."""

    def __init__(self, app, service, data_dir: str, relaunch: Optional[List[str]] = None) -> None:
        super().__init__()
        self.app = app
        self.service = service
        self.data_dir = data_dir
        self.relaunch = list(relaunch or [])
        self.monitor = None
        self.host_monitor = None
        self.presence = None
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(int(handoff.HEARTBEAT_SECONDS * 1000))
        self._heartbeat.timeout.connect(self.tick)

    def start(self) -> bool:
        """Put the icon up and start beating. False when there is no tray to use."""
        if not notify.available():
            return False
        # A stop meant for a previous tray process must not end this one.
        handoff.clear_stop(self.data_dir)
        self.app.setQuitOnLastWindowClosed(False)
        self.presence = presence.install(
            self.service,
            None,
            {
                "monitor": self.open_monitor,
                "submit": self.open_submit,
                "host_monitor": self.open_host_monitor,
                "select_job": self.select_job,
            },
            standalone=True,
            relaunch=self.relaunch,
        )
        handoff.write_heartbeat(self.data_dir, self.relaunch)
        self._heartbeat.start()
        count = len(self.service.store.active_jobs())
        tray = notify.ensure_tray()
        if tray is not None:
            try:
                tray.showMessage(
                    "MoleditPy job manager",
                    f"MoleditPy has closed; still tracking {count} job(s) from here.",
                    notify._icon(),
                    notify.TIMEOUT_MS,
                )
            except Exception:
                logging.debug("Job Manager: the hand-off note was refused", exc_info=True)
        return True

    def tick(self) -> None:
        if handoff.stop_requested(self.data_dir):
            logging.info("Job Manager: MoleditPy is back; the tray process is stopping")
            self.app.quit()
            return
        try:
            handoff.write_heartbeat(self.data_dir, self.relaunch)
        except OSError:
            logging.debug("Job Manager: heartbeat not written", exc_info=True)

    def stop(self) -> None:
        """Everything this process put up, taken down. Called after the loop ends."""
        self._heartbeat.stop()
        for window in (self.monitor, self.host_monitor):
            try:
                if window is not None:
                    window.close()
            except RuntimeError:
                pass
        presence.uninstall()
        handoff.remove_tray_file(self.data_dir)

    # --- the windows ---------------------------------------------------------

    def open_monitor(self):
        from .jobs_dialog import JobsDialog

        # A closed monitor has torn its connections down; build a new one.
        if self.monitor is None or not self.monitor.isVisible():
            self.monitor = JobsDialog(self.service)
        if self.monitor.isMinimized():
            self.monitor.showNormal()
        self.monitor.show()
        self.monitor.raise_()
        self.monitor.activateWindow()
        if self.presence is not None:
            self.presence.acknowledge()
        return self.monitor

    def open_submit(self) -> None:
        self.open_monitor().open_submit_dialog()

    def open_host_monitor(self) -> None:
        from .host_monitor import HostMonitorDialog

        if self.host_monitor is None or not self.host_monitor.isVisible():
            self.host_monitor = HostMonitorDialog(self.service)
        self.host_monitor.show()
        self.host_monitor.raise_()
        self.host_monitor.activateWindow()

    def select_job(self, job_id: str) -> None:
        self.open_monitor().select_job(job_id)


def run(app: QApplication, service, data_dir: str, relaunch: Optional[List[str]] = None) -> int:
    """The tray process's event loop. Falls back to a plain monitor without a tray."""
    tray = StandaloneTray(app, service, data_dir, relaunch)
    if not tray.start():
        # Nowhere to live but a window: show the monitor, as a plain launch does.
        tray.open_monitor()
        return app.exec()
    try:
        return app.exec()
    finally:
        tray.stop()


__all__ = ["StandaloneTray", "run"]
