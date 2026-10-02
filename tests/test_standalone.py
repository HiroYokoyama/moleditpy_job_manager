"""The tray process MoleditPy hands its jobs to, and taking them back."""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import job_manager  # noqa: E402
from job_manager import handoff, notify, presence  # noqa: E402
from job_manager.models import STATE_RUNNING, Job  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.standalone import StandaloneTray, run  # noqa: E402
from job_manager.store import JobStore  # noqa: E402


class StandaloneTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.dir = tempfile.mkdtemp(prefix="standalone_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        original = self.app.quitOnLastWindowClosed()
        self.addCleanup(self.app.setQuitOnLastWindowClosed, original)
        self.tray_icon = MagicMock()
        patcher = patch("job_manager.notify.QSystemTrayIcon", return_value=self.tray_icon)
        self.tray_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.tray_class.isSystemTrayAvailable.return_value = True
        self.addCleanup(notify.shutdown)
        self.addCleanup(presence.uninstall)

    def service(self, *jobs) -> JobService:
        store = JobStore(self.dir)
        store.jobs = {job.id: job for job in jobs}
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        return service

    def standalone(self, *jobs, relaunch=None) -> StandaloneTray:
        tray = StandaloneTray(self.app, self.service(*jobs), self.dir, relaunch)
        self.addCleanup(tray.stop)
        return tray


class TestStarting(StandaloneTestCase):
    def test_it_beats_and_lives_in_the_tray(self):
        tray = self.standalone(
            Job(host_id="h1", scheduler="slurm", state=STATE_RUNNING), relaunch=["moleditpy"]
        )
        self.assertTrue(tray.start())

        self.assertEqual(handoff.live_tray(self.dir)["relaunch"], ["moleditpy"])
        # No window: closing the monitor it opens later must not end it.
        self.assertFalse(self.app.quitOnLastWindowClosed())
        self.assertTrue(presence.current().tray.standalone)
        message = self.tray_icon.showMessage.call_args[0][1]
        self.assertIn("1 job", message)

    def test_a_stop_left_for_an_earlier_process_is_ignored(self):
        handoff.request_stop(self.dir)
        tray = self.standalone()
        tray.start()
        self.assertFalse(handoff.stop_requested(self.dir))

    def test_without_a_tray_it_says_so(self):
        self.tray_class.isSystemTrayAvailable.return_value = False
        self.assertFalse(self.standalone().start())
        self.assertIsNone(handoff.live_tray(self.dir))

    def test_without_a_tray_run_shows_the_monitor_instead(self):
        self.tray_class.isSystemTrayAvailable.return_value = False
        service = self.service()
        with (
            patch.object(StandaloneTray, "open_monitor") as monitor,
            patch.object(self.app, "exec", return_value=0),
        ):
            self.assertEqual(run(self.app, service, self.dir), 0)
        monitor.assert_called_once()


class TestTheHeartbeat(StandaloneTestCase):
    def test_a_tick_refreshes_it(self):
        tray = self.standalone()
        tray.start()
        os.remove(handoff.tray_path(self.dir))
        tray.tick()
        self.assertIsNotNone(handoff.live_tray(self.dir))

    def test_a_stop_request_ends_the_process(self):
        tray = self.standalone()
        tray.start()
        handoff.request_stop(self.dir)
        with patch.object(self.app, "quit") as quit_app:
            tray.tick()
        quit_app.assert_called_once()

    def test_stopping_takes_everything_down(self):
        tray = self.standalone()
        tray.start()
        tray.stop()
        self.assertIsNone(handoff.live_tray(self.dir))
        self.assertIsNone(presence.current())

    def test_run_stops_when_the_loop_ends(self):
        service = self.service()
        with patch.object(self.app, "exec", return_value=0):
            run(self.app, service, self.dir, ["moleditpy"])
        self.assertIsNone(handoff.live_tray(self.dir))


class TestItsWindows(StandaloneTestCase):
    def test_the_monitor_opens_and_is_reused_while_open(self):
        tray = self.standalone()
        tray.start()
        first = tray.open_monitor()
        self.addCleanup(first.close)
        self.assertIs(tray.open_monitor(), first)

    def test_a_closed_monitor_is_rebuilt(self):
        tray = self.standalone()
        tray.start()
        first = tray.open_monitor()
        first.close()
        second = tray.open_monitor()
        self.addCleanup(second.close)
        self.assertIsNot(second, first)

    def test_opening_it_acknowledges_failures(self):
        tray = self.standalone()
        tray.start()
        tray.presence.unseen_failures = 2
        self.addCleanup(tray.open_monitor().close)
        self.assertEqual(tray.presence.unseen_failures, 0)

    def test_the_menu_reaches_its_windows(self):
        job = Job(host_id="h1", scheduler="slurm", state=STATE_RUNNING)
        tray = self.standalone(job)
        tray.start()
        actions = presence.current().tray.actions
        with patch.object(tray, "open_monitor") as monitor:
            actions["select_job"](job.id)
            actions["submit"]()
        self.assertEqual(monitor.call_count, 2)
        monitor.return_value.select_job.assert_called_once_with(job.id)
        monitor.return_value.open_submit_dialog.assert_called_once()

    def test_the_host_monitor_opens(self):
        tray = self.standalone()
        tray.start()
        with patch("job_manager.host_monitor.HostMonitorDialog") as dialog:
            tray.open_host_monitor()
            tray.open_host_monitor()
        dialog.assert_called_once()


class TestTheCommandLine(StandaloneTestCase):
    def main(self, *args):
        from job_manager import __main__ as entry

        with (
            patch.object(sys, "argv", ["__main__.py", *args]),
            patch("job_manager.standalone.run", return_value=0) as tray_run,
            patch("job_manager._take_tracking_back") as take_back,
            patch("job_manager.jobs_dialog.JobsDialog"),
            patch.object(self.app, "exec", return_value=0),
        ):
            entry.main()
        return tray_run, take_back

    def test_tray_mode_carries_the_way_back(self):
        tray_run, take_back = self.main("--tray", "--relaunch", '["moleditpy", "--x"]')
        self.assertEqual(tray_run.call_args[0][3], ["moleditpy", "--x"])
        take_back.assert_not_called()

    def test_a_garbled_way_back_is_dropped(self):
        tray_run, _ = self.main("--tray", "--relaunch", "not json")
        self.assertEqual(tray_run.call_args[0][3], [])

    def test_opening_it_by_hand_stops_a_tray_process(self):
        tray_run, take_back = self.main()
        tray_run.assert_not_called()
        take_back.assert_called_once()


class TestMoleditPyTakesItBack(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="takeback_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        previous = os.environ.get("MOLEDITPY_JOB_MANAGER_DIR")
        os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = self.dir

        def restore():
            if previous is None:
                os.environ.pop("MOLEDITPY_JOB_MANAGER_DIR", None)
            else:
                os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = previous
            job_manager._context = None

        self.addCleanup(restore)
        importlib.reload(job_manager)
        self.addCleanup(job_manager.shutdown)

    def test_loading_the_plugin_stops_the_tray_process_before_reading_jobs(self):
        order = []
        with (
            patch(
                "job_manager.handoff.stop_running_tray",
                side_effect=lambda d: order.append(("stop", d)),
            ),
            patch.object(
                job_manager, "_startup_store", side_effect=lambda: order.append(("read",))
            ),
        ):
            context = MagicMock()
            context.get_window.return_value = None
            job_manager.initialize(context)
        self.assertEqual(order, [("stop", self.dir), ("read",)])

    def test_a_failure_there_does_not_stop_the_load(self):
        with patch("job_manager.handoff.stop_running_tray", side_effect=OSError("x")):
            context = MagicMock()
            context.get_window.return_value = None
            job_manager.initialize(context)  # must not raise


if __name__ == "__main__":
    unittest.main()
