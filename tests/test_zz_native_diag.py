"""TEMPORARY: isolate the access violation seen on the real windows platform."""

import os
import subprocess
import sys
import textwrap

import pytest

PRE = """
import os, sys, tempfile, time
sys.path.insert(0, os.getcwd())
os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = tempfile.mkdtemp()
from PyQt6.QtWidgets import QApplication, QDialog
app = QApplication([])
def pump(s=1.5):
    end = time.monotonic() + s
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.02)
"""

SCENARIOS = {
    "plain_dialog": "d = QDialog(); d.show(); pump()",
    "icon_dialog": "from job_manager.window_utils import make_independent\nd = QDialog(); make_independent(d); d.show(); pump()",
    "passthrough_nativeEvent": textwrap.dedent('''
        class D(QDialog):
            def nativeEvent(self, t, m):
                return super().nativeEvent(t, m)
        d = D(); d.show(); pump()
    '''),
    "true0_nativeEvent_never": textwrap.dedent('''
        class D(QDialog):
            def nativeEvent(self, t, m):
                if False:
                    return True, 0
                return False, 0
        d = D(); d.show(); pump()
    '''),
    "msg_read_nativeEvent": textwrap.dedent('''
        from ctypes import wintypes
        class D(QDialog):
            def nativeEvent(self, t, m):
                if t == b"windows_generic_MSG" and m:
                    wintypes.MSG.from_address(int(m)).message
                return super().nativeEvent(t, m)
        d = D(); d.show(); pump()
    '''),
    "windowtaskbar_on_plain": textwrap.dedent('''
        from job_manager import win_taskbar
        from PyQt6.QtWidgets import QStyle
        class D(QDialog):
            def nativeEvent(self, t, m):
                if t == b"windows_generic_MSG":
                    if self.bar.handle(m):
                        return True, 0
                return super().nativeEvent(t, m)
        d = D()
        icon = d.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        d.bar = win_taskbar.WindowTaskbar(d, [(1, icon, "x", lambda: None)])
        d.show(); pump()
        print("hicons", d.bar._icons)
    '''),
    "jobs_dialog_no_native": textwrap.dedent('''
        from job_manager.jobs_dialog import JobsDialog
        from job_manager.service import JobService
        from job_manager.store import JobStore
        del JobsDialog.nativeEvent
        d = JobsDialog(JobService(store=JobStore(os.environ["MOLEDITPY_JOB_MANAGER_DIR"])))
        d.show(); pump()
    '''),
    "jobs_dialog_full": textwrap.dedent('''
        from job_manager.jobs_dialog import JobsDialog
        from job_manager.service import JobService
        from job_manager.store import JobStore
        d = JobsDialog(JobService(store=JobStore(os.environ["MOLEDITPY_JOB_MANAGER_DIR"])))
        d.show(); pump()
    '''),
}


@pytest.mark.skipif(not os.environ.get("JM_NATIVE_DIAG"), reason="diag step only")
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario(name):
    env = dict(os.environ, QT_QPA_PLATFORM="windows")
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", PRE + SCENARIOS[name]],
        capture_output=True, text=True, env=env, timeout=60,
    )
    print(f"\n=== {name}: rc={result.returncode}\n{result.stdout[-1500:]}\n{result.stderr[-3000:]}")
