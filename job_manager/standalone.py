"""The Job Manager run without MoleditPy: a monitor opened by hand, or the
tray process MoleditPy hands its jobs to when it closes.

A monitor opened by hand (``python -m job_manager``, ``launch_job_manager.bat``)
first looks for a Job Manager already running -- in a MoleditPy, as another
standalone monitor, or as the tray process -- and, finding one, asks it to bring
its window up and exits. Starting a second tracker beside it would query every
host twice and announce every job ending twice. See :mod:`job_manager.instances`.

The tray process (``__main__.py --tray``, started by :mod:`job_manager.handoff`)
opens no window of its own: the tray icon is its whole interface until
something is clicked. Results are still downloaded, but not opened -- there is
no MoleditPy to open them in. It ends when asked from its menu, or when a
MoleditPy starting up asks it to, so the two never track the same jobs at once.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

from . import instances, notify, presence
from .beacon import InstanceBeacon
from .window_utils import bring_to_front, exec_once


class _StandaloneWindows(QObject):
    """The windows a Job Manager without MoleditPy can show, one of each."""

    def __init__(self, app, service, data_dir: str) -> None:
        super().__init__()
        self.app = app
        self.service = service
        self.data_dir = data_dir
        self.monitor = None
        self.host_monitor = None
        self.presence = None
        self.beacon: Optional[InstanceBeacon] = None

    def _start_beacon(self, role: str, extra=None) -> None:
        handlers = {
            instances.ACTION_SHOW_MONITOR: self.open_monitor,
            instances.ACTION_SHOW_HOST_MONITOR: self.open_host_monitor,
        }
        handlers.update(extra or {})
        self.beacon = InstanceBeacon(self.data_dir, role, handlers)
        self.beacon.start()

    def open_monitor(self):
        from .jobs_dialog import JobsDialog

        # A closed monitor has torn its connections down; build a new one.
        if self.monitor is None or not self.monitor.isVisible():
            self.monitor = JobsDialog(self.service)
        bring_to_front(self.monitor)
        if self.presence is not None:
            self.presence.acknowledge()
        return self.monitor

    def open_submit(self) -> None:
        self.open_monitor().open_submit_dialog()

    def open_host_monitor(self):
        from .host_monitor import HostMonitorDialog, find_open

        if self.host_monitor is None or not self.host_monitor.isVisible():
            # The job monitor's own button may have opened one already.
            self.host_monitor = find_open(self.service) or HostMonitorDialog(self.service)
        bring_to_front(self.host_monitor)
        return self.host_monitor

    def open_settings(self) -> None:
        from .settings_dialog import SettingsDialog

        # No local API from here: MoleditPy serves it.
        exec_once("settings", lambda: SettingsDialog(self.service, None, standalone=True))

    def select_job(self, job_id: str) -> None:
        self.open_monitor().select_job(job_id)

    def start_web(self, wait_for_port: bool = False) -> None:
        from . import web_service

        try:
            web_service.resume(self.service, wait_for_port=wait_for_port)
        except Exception:
            logging.warning("Job Manager: the web monitor did not start", exc_info=True)

    def stop(self) -> None:
        """Everything this process put up, taken down. Called after the loop ends."""
        from . import web_service

        web_service.shutdown_for(self.service)
        if self.beacon is not None:
            self.beacon.stop()
            self.beacon = None
        for window in (self.monitor, self.host_monitor):
            try:
                if window is not None:
                    window.close()
            except RuntimeError:
                pass
        presence.uninstall()


class StandaloneMonitor(_StandaloneWindows):
    """A monitor opened by hand. Ends when its last window closes.

    Its tray menu reaches its own windows -- the plugin's version of the menu
    asks a MoleditPy that is not there. Closing it hands the jobs to the tray
    process exactly as closing MoleditPy does, when that is switched on.
    """

    def start(self, host_view: bool = False) -> None:
        self.presence = presence.install(
            self.service,
            None,
            {
                "monitor": self.open_monitor,
                "submit": self.open_submit,
                "host_monitor": self.open_host_monitor,
                "select_job": self.select_job,
                "settings": self.open_settings,
            },
            quit_label="Quit Job Manager",
        )
        # Opened by hand is opened: see TrayController.keep_running.
        if self.presence.tray is not None:
            self.presence.tray.mark_opened()
        self._start_beacon(instances.ROLE_STANDALONE)
        if host_view:
            self.open_host_monitor()
        else:
            self.open_monitor()


class StandaloneTray(_StandaloneWindows):
    """The tray process: a tray icon and whatever it is asked to open."""

    def __init__(
        self,
        app,
        service,
        data_dir: str,
        relaunch: Optional[List[str]] = None,
        after_pid: int = 0,
    ) -> None:
        super().__init__(app, service, data_dir)
        self.relaunch = list(relaunch or [])
        #: The process handing over: still running, and registered, as this starts.
        self.after_pid = int(after_pid or 0)

    def already_tracked(self) -> bool:
        """Another standalone monitor or tray process is running: nothing to do."""
        return instances.standalone_running(self.data_dir, self.after_pid)

    def start(self) -> bool:
        """Put the icon up and start beating. False when there is no tray to use."""
        if not notify.available():
            return False
        self.app.setQuitOnLastWindowClosed(False)
        self.presence = presence.install(
            self.service,
            None,
            {
                "monitor": self.open_monitor,
                "submit": self.open_submit,
                "host_monitor": self.open_host_monitor,
                "select_job": self.select_job,
                "settings": self.open_settings,
            },
            standalone=True,
            relaunch=self.relaunch,
        )
        self._start_beacon(instances.ROLE_TRAY, {instances.ACTION_STOP: self.app.quit})
        count = len(self.service.store.active_jobs())
        notify.notify(
            "MoleditPy job manager",
            f"Still tracking {count} job(s) from here."
            if count
            else "Still running here, in the tray.",
        )
        return True

    def start_as_window(self) -> None:
        """Nowhere to live but a window: a monitor, as a plain launch opens."""
        self._start_beacon(instances.ROLE_STANDALONE)
        self.open_monitor()


def run(
    app: QApplication,
    service,
    data_dir: str,
    relaunch: Optional[List[str]] = None,
    after_pid: int = 0,
) -> int:
    """The tray process's event loop. Falls back to a plain monitor without a tray."""
    tray = StandaloneTray(app, service, data_dir, relaunch, after_pid)
    if tray.already_tracked():
        logging.info("Job Manager: another Job Manager is tracking already; not starting")
        return 0
    if not tray.start():
        tray.start_as_window()
    # The MoleditPy handing over may still hold the port the saved links name.
    tray.start_web(wait_for_port=True)
    try:
        return app.exec()
    finally:
        tray.stop()


def run_monitor(app: QApplication, service, data_dir: str, host_view: bool = False) -> int:
    """A monitor opened by hand, already known to be the only one running."""
    windows = StandaloneMonitor(app, service, data_dir)
    windows.start(host_view)
    windows.start_web()
    try:
        return app.exec()
    finally:
        windows.stop()


__all__ = ["StandaloneMonitor", "StandaloneTray", "run", "run_monitor"]
