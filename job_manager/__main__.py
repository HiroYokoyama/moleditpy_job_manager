"""Standalone entry point when run as `python -m job_manager` or `python __main__.py`."""

from __future__ import annotations

import os
import sys

if __package__ is None or __package__ == "":
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    pkg_name = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    __package__ = pkg_name

from PyQt6.QtWidgets import QApplication

from . import PLUGIN_VERSION, get_service, instances
from . import shutdown as release_service
from .store import default_data_dir

HOST_MONITOR_FLAGS = ("--host-monitor", "--hosts", "host-monitor", "-m")


def _argument(args, name: str) -> str:
    if name in args[:-1]:
        return args[args.index(name) + 1]
    return ""


def main() -> int:
    args = sys.argv[1:]
    data_dir = default_data_dir()
    tray_mode = "--tray" in args
    host_view = any(arg in HOST_MONITOR_FLAGS for arg in args)

    if tray_mode:
        try:
            after_pid = int(_argument(args, "--after-pid") or 0)
        except ValueError:
            after_pid = 0
        # Before anything is built: a tray process with nothing to do should
        # not first read the job list, put an icon up and take it down again.
        if instances.standalone_running(data_dir, after_pid):
            return 0
    else:
        # One Job Manager at a time, when it is ours to choose: a launch beside
        # one already running brings that one's window up instead of starting
        # a second tracker. Before the QApplication, so nothing flashes.
        action = instances.ACTION_SHOW_HOST_MONITOR if host_view else instances.ACTION_SHOW_MONITOR
        running = instances.defer_to_running(data_dir, action)
        if running is not None:
            print(
                f"Job Manager is already running (pid {running['pid']}, {running['role']}); "
                "its window has been brought up.",
                file=sys.stderr,
            )
            return 0
        # A tray process from 2.0 or 2.1 is not in the registry; it only knows
        # its own stop file, and would otherwise track beside this window.
        from . import handoff

        handoff.stop_running_tray(data_dir)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(f"Job Manager {PLUGIN_VERSION}")

    # get_service(), not JobService(): it is the only thing that connects
    # job_finished to the notifier, so building the service directly ran a
    # standalone monitor with neither the desktop notification nor the chat
    # message a job ending is supposed to produce. There is no PluginContext
    # here, which the status-bar counter it also installs allows for.
    service = get_service()
    try:
        if tray_mode:
            # Started by MoleditPy, or a standalone monitor, as it closed: see
            # job_manager/handoff.py.
            import json

            from .standalone import run

            try:
                relaunch = [str(part) for part in json.loads(_argument(args, "--relaunch") or "[]")]
            except (ValueError, TypeError):
                relaunch = []
            return run(app, service, data_dir, relaunch, after_pid)

        from .standalone import run_monitor

        return run_monitor(app, service, data_dir, host_view)
    finally:
        # The module's own teardown, which also takes away the tray icon the
        # notifier put up and the badge on the task bar icon.
        release_service()


if __name__ == "__main__":
    sys.exit(main())
