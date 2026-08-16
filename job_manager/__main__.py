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

from . import PLUGIN_VERSION
from .jobs_dialog import JobsDialog
from .service import JobService
from .theme import apply_theme



def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(f"Job Manager {PLUGIN_VERSION}")

    service = JobService()

    dialog = JobsDialog(service)
    apply_theme(dialog)
    dialog.show()

    return app.exec()



if __name__ == "__main__":
    sys.exit(main())
