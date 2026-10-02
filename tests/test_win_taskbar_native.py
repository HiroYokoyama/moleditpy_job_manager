"""The task bar and tray against a real Windows desktop.

Everything else runs on the offscreen platform, where a window has no HWND
the shell knows and there is no tray: the COM calls, the native message
dispatch and the tray would be tested only through stand-ins. The Windows CI
job runs this file a second time with ``QT_QPA_PLATFORM=windows`` and fails if
it skipped, so the ctypes vtable slots and the WM_COMMAND route are exercised
for real.
"""

from __future__ import annotations

import ctypes
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from job_manager import handoff, notify, presence, win_taskbar  # noqa: E402
from job_manager.icon import plugin_icon  # noqa: E402
from job_manager.models import STATE_RUNNING, Job  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

_app = QApplication.instance() or QApplication([])
NATIVE = sys.platform == "win32" and _app.platformName() == "windows"

pytestmark = pytest.mark.skipif(
    not NATIVE, reason="needs Windows with QT_QPA_PLATFORM=windows (the Windows CI job's own step)"
)


def wait_until(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def send(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> None:
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    ]
    user32.SendMessageW(hwnd, message, wparam, lparam)


class NativeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="win_taskbar_native_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def window(self) -> QWidget:
        widget = QWidget()
        widget.setWindowTitle("job manager task bar test")
        widget.resize(200, 100)
        widget.show()
        self.addCleanup(widget.close)
        self.assertTrue(wait_until(widget.isVisible))
        return widget

    def service(self, *jobs) -> JobService:
        store = JobStore(self.tmp)
        store.jobs = {job.id: job for job in jobs}
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        return service


class TestTheComObject(NativeTestCase):
    def test_itaskbarlist3_is_created_and_initialised(self):
        self.assertIsNotNone(win_taskbar._instance())

    def test_every_progress_state_is_accepted_on_a_real_window(self):
        # Each call goes through its own vtable slot; a wrong slot would call
        # the wrong method with the wrong arguments and fail or crash here.
        hwnd = int(self.window().winId())
        for state, value, total in (
            ("indeterminate", 0, 0),
            ("normal", 1, 3),
            ("paused", 2, 3),
            ("error", 3, 3),
            ("none", 0, 0),
        ):
            self.assertTrue(win_taskbar.set_progress(hwnd, state, value, total), state)

    def test_the_registered_message_exists(self):
        self.assertGreaterEqual(win_taskbar.button_created_message(), 0xC000)

    def test_an_icon_is_made_from_png(self):
        hicon = win_taskbar._hicon_for(plugin_icon())
        self.assertNotEqual(hicon, 0)
        ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(hicon))


class TestTheBadge(NativeTestCase):
    """ "Show the count on the app icon": Qt draws it as the task bar button's
    overlay icon. Whether Explorer then shows it is a Windows setting
    (Taskbar > Show badges on taskbar apps); what is ours is that Qt takes it."""

    def test_the_count_is_accepted_and_cleared_on_a_real_window(self):
        from job_manager import taskbar

        self.window()
        self.assertTrue(taskbar.SUPPORTED)
        self.assertTrue(taskbar.set_badge(3))
        QApplication.processEvents()
        self.assertTrue(taskbar.set_badge(120))
        self.assertTrue(taskbar.clear_badge())

    def test_a_window_opened_after_the_badge_does_not_break(self):
        from job_manager import taskbar

        self.assertTrue(taskbar.set_badge(2))
        self.addCleanup(taskbar.clear_badge)
        self.window()
        QApplication.processEvents()


class TestTheMonitorWindow(NativeTestCase):
    def dialog(self):
        from job_manager.jobs_dialog import JobsDialog

        service = self.service(Job(host_id="h1", scheduler="slurm", state=STATE_RUNNING))
        dialog = JobsDialog(service)
        dialog.show()
        self.addCleanup(dialog.close)
        self.assertTrue(wait_until(dialog.isVisible))
        return service, dialog

    def test_a_thumbnail_click_reaches_the_dialog(self):
        # The whole route: a native WM_COMMAND into Qt's window procedure,
        # nativeEvent, the MSG read by address, and the button's callback.
        service, dialog = self.dialog()
        hwnd = int(dialog.winId())
        with patch.object(service.poller, "refresh_now", return_value=True) as refresh:
            send(hwnd, win_taskbar.WM_COMMAND, (win_taskbar.THBN_CLICKED << 16) | 1)
        refresh.assert_called_once()

    def test_an_ordinary_command_is_left_to_qt(self):
        service, dialog = self.dialog()
        with patch.object(service.poller, "refresh_now") as refresh:
            send(int(dialog.winId()), win_taskbar.WM_COMMAND, 1)
        refresh.assert_not_called()

    def test_the_button_being_created_adds_the_thumbnail_buttons(self):
        _service, dialog = self.dialog()
        with patch.object(
            win_taskbar, "add_thumb_buttons", wraps=win_taskbar.add_thumb_buttons
        ) as add:
            send(int(dialog.winId()), win_taskbar.button_created_message())
        add.assert_called_once()
        hwnd, buttons = add.call_args[0][:2]
        self.assertEqual(hwnd, int(dialog.winId()))
        self.assertEqual([button[0] for button in buttons], [1, 2, 3])
        # Real icons, or the buttons are blank squares.
        self.assertTrue(all(button[1] for button in buttons))

    def test_a_job_ending_flashes_a_minimised_window_without_raising(self):
        service, dialog = self.dialog()
        with patch("job_manager.notify.available", return_value=False):
            shown = presence.Presence(service, dialog)
        self.addCleanup(shown.detach)
        dialog.showMinimized()
        QApplication.processEvents()
        with patch.object(QApplication, "alert", wraps=QApplication.alert) as alert:
            service.job_finished.emit("x", "DONE")
        alert.assert_called_once_with(dialog, 0)


class TestTheRealTray(NativeTestCase):
    def setUp(self):
        super().setUp()
        if not notify.available():
            self.skipTest("this desktop session has no notification area")
        self.addCleanup(notify.shutdown)
        self.original_quit = _app.quitOnLastWindowClosed()
        self.addCleanup(_app.setQuitOnLastWindowClosed, self.original_quit)

    def test_the_tray_gets_its_menu_and_keep_running_applies(self):
        service = self.service(Job(host_id="h1", scheduler="slurm", state=STATE_RUNNING))
        service.store.set_pref("keep_running_in_tray", True)
        main = self.window()
        # The in-process fallback, which is the path that touches Qt's quit flag.
        with patch("job_manager.handoff.can_hand_off", return_value=False):
            shown = presence.Presence(service, main)
        self.addCleanup(shown.detach)

        self.assertIsNotNone(shown.tray.tray)
        self.assertIs(shown.tray.tray.contextMenu(), shown.tray.menu)
        self.assertIn("1 running", shown.tray.tray.toolTip())
        self.assertFalse(_app.quitOnLastWindowClosed())

        shown.detach()
        self.assertEqual(_app.quitOnLastWindowClosed(), self.original_quit)


class TestTheHandOffForReal(NativeTestCase):
    """The tray process itself: started as MoleditPy starts it, stopped as
    MoleditPy stops it. pythonw, a detached process, the real tray."""

    def test_it_starts_beats_and_stops_when_asked(self):
        import os
        import subprocess

        if not notify.available():
            self.skipTest("this desktop session has no notification area")
        store = JobStore(self.tmp)
        job = Job(host_id="h1", scheduler="slurm", state=STATE_RUNNING)
        store.jobs = {job.id: job}
        store.save_jobs()
        package_dir = os.path.dirname(os.path.abspath(presence.__file__))
        command = handoff.standalone_command(package_dir, ["moleditpy"])
        env = dict(os.environ, MOLEDITPY_JOB_MANAGER_DIR=self.tmp, QT_QPA_PLATFORM="windows")
        process = subprocess.Popen(command, cwd=os.path.dirname(package_dir), env=env)
        self.addCleanup(process.kill)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and handoff.live_tray(self.tmp) is None:
            self.assertIsNone(process.poll(), "the tray process exited on its own")
            time.sleep(0.2)
        beat = handoff.live_tray(self.tmp)
        self.assertIsNotNone(beat, "no heartbeat within a minute")
        self.assertEqual(beat["relaunch"], ["moleditpy"])

        self.assertTrue(handoff.stop_running_tray(self.tmp, timeout=30))
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertFalse(os.path.exists(handoff.tray_path(self.tmp)))


if __name__ == "__main__":
    unittest.main()
