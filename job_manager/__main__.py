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

from . import PLUGIN_VERSION, get_service
from . import shutdown as release_service
from .jobs_dialog import JobsDialog


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(f"Job Manager {PLUGIN_VERSION}")

    # get_service(), not JobService(): it is the only thing that connects
    # job_finished to the notifier, so building the service directly ran a
    # standalone monitor with neither the desktop notification nor the chat
    # message a job ending is supposed to produce. There is no PluginContext
    # here, which the status-bar counter it also installs allows for.
    args = sys.argv[1:]
    if "--tray" not in args:
        # Opened by hand while a tray process tracks the same list: one of
        # the two has to stop, and the window the user just opened wins.
        from . import _take_tracking_back

        _take_tracking_back()
    service = get_service()
    try:
        if "--tray" in args:
            # Started by MoleditPy as it closed: see job_manager/handoff.py.
            import json

            from .standalone import run
            from .store import default_data_dir

            relaunch = []
            if "--relaunch" in args[:-1]:
                try:
                    relaunch = [
                        str(part) for part in json.loads(args[args.index("--relaunch") + 1])
                    ]
                except (ValueError, TypeError):
                    relaunch = []
            return run(app, service, default_data_dir(), relaunch)
        if any(arg in ("--host-monitor", "--hosts", "host-monitor", "-m") for arg in args):
            from .host_monitor import HostMonitorDialog

            dialog = HostMonitorDialog(service)
        else:
            dialog = JobsDialog(service)
        dialog.show()

        return app.exec()
    finally:
        # The module's own teardown, which also takes away the tray icon the
        # notifier put up and the badge on the task bar icon.
        release_service()


if __name__ == "__main__":
    sys.exit(main())
