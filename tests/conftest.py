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
import shutil
import tempfile

# Python falls back to os.getcwd() when TMP/TEMP/TMPDIR are all unset, and the
# working directory of a test run is the repository. Every mkdtemp() in the
# suite then landed in the source tree -- which is why .gitignore carries a
# rule per test-module prefix. Pinned once here so a shell without a temp
# directory cannot scatter a run across the checkout.
if os.path.abspath(tempfile.gettempdir()) == os.path.abspath(os.getcwd()):
    tempfile.tempdir = os.path.abspath(
        os.environ.get("RUNNER_TEMP") or os.path.expanduser("~/.cache/moleditpy_job_manager_tests")
    )
    os.makedirs(tempfile.tempdir, exist_ok=True)

# Many tests mkdtemp() without removing it, and the shells they start mktemp
# on their own; a run left a few hundred entries in the system temp directory.
# Everything a run creates -- in this process and in the processes it starts --
# goes under one directory that is removed when the session ends.
_SESSION_TMP = tempfile.mkdtemp(prefix="moleditpy_job_manager_tests_")
tempfile.tempdir = _SESSION_TMP
for _name in ("TMP", "TEMP", "TMPDIR"):
    os.environ[_name] = _SESSION_TMP

os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = tempfile.mkdtemp(prefix="data_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - PyQt6-less environments (CI)
    QApplication = None

if QApplication is not None:
    _app = QApplication.instance() or QApplication([])


def pytest_sessionfinish(session, exitstatus):
    # A runner a test detached may still hold a file; what it holds is left.
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)
