"""Shared test setup.

Two things have to happen before any ``job_manager`` import:

1. Point the plugin's data directory at a throwaway location. Without it the
   suite would read and write the developer's real
   ``~/.moleditpy/job_manager/jobs.json``.
2. Make Qt headless and create the single QApplication that must outlive every
   test -- when PyQt6 is installed at all. CI installs only pytest, so the Qt
   test modules skip themselves there.
"""

import os
import tempfile

os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = tempfile.mkdtemp(prefix="moleditpy_job_manager_tests_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - PyQt6-less environments (CI)
    QApplication = None

if QApplication is not None:
    _app = QApplication.instance() or QApplication([])
