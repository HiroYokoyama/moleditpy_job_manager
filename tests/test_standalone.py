"""The Job Manager without MoleditPy: the tray process, a monitor opened by
hand, one at a time, and MoleditPy taking the jobs back."""

from __future__ import annotations

import importlib
import json
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
from job_manager import instances, notify, presence  # noqa: E402
from job_manager.models import STATE_RUNNING, Job  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.standalone import StandaloneMonitor, StandaloneTray, run  # noqa: E402
from job_manager.store import JobStore  # noqa: E402


def fake_instance(directory, pid, role, beat=None):
    """Another Job Manager's heartbeat, as that process would have written it."""
    import time

    folder = os.path.join(directory, instances.INSTANCES_DIR)
    os.makedirs(folder, exist_ok=True)
    now = time.time() if beat is None else beat
    with open(os.path.join(folder, f"{pid}.json"), "w", encoding="utf-8") as handle:
        json.dump({"pid": pid, "role": role, "beat": now, "started": now}, handle)


def mine(directory):
    """This process's own registry entry, if it has one."""
    path = os.path.join(directory, instances.INSTANCES_DIR, f"{os.getpid()}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


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
    def test_it_registers_and_lives_in_the_tray(self):
        tray = self.standalone(
            Job(host_id="h1", scheduler="slurm", state=STATE_RUNNING), relaunch=["moleditpy"]
        )
        self.assertTrue(tray.start())

        self.assertEqual(mine(self.dir)["role"], instances.ROLE_TRAY)
        # No window: closing the monitor it opens later must not end it.
        self.assertFalse(self.app.quitOnLastWindowClosed())
        self.assertTrue(presence.current().tray.standalone)
        self.assertEqual(presence.current().tray.relaunch, ["moleditpy"])
        message = self.tray_icon.showMessage.call_args[0][1]
        self.assertIn("1 job", message)

    def test_with_no_job_it_still_says_where_it_went(self):
        self.assertTrue(self.standalone().start())
        message = self.tray_icon.showMessage.call_args[0][1]
        self.assertIn("in the tray", message)

    def test_run_serves_the_web_view_when_it_was_left_on(self):
        from job_manager import web_service

        service = self.service()
        service.store.set_pref("host_monitor_web", True)
        seen = {}

        def exec_():
            seen["running"] = web_service.for_service(service).running
            return 0

        with (
            patch.object(self.app, "exec", side_effect=exec_),
            patch.object(web_service, "PORT_WAIT_SECONDS", 0),
        ):
            run(self.app, service, self.dir, ["moleditpy"])
        self.assertTrue(seen["running"])
        # And the process ending takes the socket, not the choice.
        self.assertFalse(web_service.for_service(service).running)
        self.assertTrue(service.store.get_pref("host_monitor_web", False))

    def test_without_a_tray_it_says_so(self):
        self.tray_class.isSystemTrayAvailable.return_value = False
        self.assertFalse(self.standalone().start())
        self.assertIsNone(mine(self.dir))

    def test_without_a_tray_run_shows_the_monitor_instead(self):
        self.tray_class.isSystemTrayAvailable.return_value = False
        service = self.service()
        with (
            patch.object(StandaloneTray, "open_monitor") as monitor,
            patch.object(self.app, "exec", return_value=0),
        ):
            self.assertEqual(run(self.app, service, self.dir), 0)
        monitor.assert_called_once()


class TestOnlyOneTracker(StandaloneTestCase):
    """The tray process steps aside for a Job Manager already tracking."""

    def test_another_tray_process_means_nothing_to_do(self):
        fake_instance(self.dir, 4242, instances.ROLE_TRAY)
        service = self.service()
        with patch.object(self.app, "exec") as loop:
            self.assertEqual(run(self.app, service, self.dir), 0)
        loop.assert_not_called()
        self.assertIsNone(mine(self.dir))

    def test_a_standalone_monitor_means_nothing_to_do(self):
        fake_instance(self.dir, 4242, instances.ROLE_STANDALONE)
        self.assertTrue(StandaloneTray(self.app, self.service(), self.dir).already_tracked())

    def test_the_one_handing_over_is_not_counted(self):
        # It is still running, and registered, while the tray process starts.
        fake_instance(self.dir, 4242, instances.ROLE_STANDALONE)
        tray = StandaloneTray(self.app, self.service(), self.dir, after_pid=4242)
        self.assertFalse(tray.already_tracked())

    def test_a_moleditpy_is_not_counted(self):
        # Two MoleditPy windows each run the plugin, and that is accepted; a
        # second one quitting must still be able to hand over.
        fake_instance(self.dir, 4242, instances.ROLE_MOLEDITPY)
        self.assertFalse(StandaloneTray(self.app, self.service(), self.dir).already_tracked())


class TestAnsweringOtherLaunches(StandaloneTestCase):
    def test_a_stop_request_ends_the_process(self):
        tray = self.standalone()
        tray.start()
        self.assertEqual(tray.beacon.handlers[instances.ACTION_STOP], self.app.quit)
        stop = MagicMock()
        tray.beacon.handlers[instances.ACTION_STOP] = stop
        instances.send_request(self.dir, os.getpid(), instances.ACTION_STOP)
        tray.beacon.check_requests()
        stop.assert_called_once()

    def test_a_show_request_opens_the_monitor(self):
        tray = self.standalone()
        tray.start()
        instances.send_request(self.dir, os.getpid(), instances.ACTION_SHOW_MONITOR)
        with patch.object(tray, "open_monitor") as monitor:
            tray.beacon.handlers[instances.ACTION_SHOW_MONITOR] = monitor
            tray.beacon.check_requests()
        monitor.assert_called_once()
        self.assertFalse(instances.request_pending(self.dir, os.getpid()))

    def test_stopping_takes_everything_down(self):
        tray = self.standalone()
        tray.start()
        tray.stop()
        self.assertIsNone(mine(self.dir))
        self.assertIsNone(presence.current())

    def test_run_stops_when_the_loop_ends(self):
        service = self.service()
        with patch.object(self.app, "exec", return_value=0):
            run(self.app, service, self.dir, ["moleditpy"])
        self.assertIsNone(mine(self.dir))


class TestAMonitorOpenedByHand(StandaloneTestCase):
    def monitor(self) -> StandaloneMonitor:
        windows = StandaloneMonitor(self.app, self.service(), self.dir)
        self.addCleanup(windows.stop)
        return windows

    def test_it_registers_and_opens_the_monitor(self):
        windows = self.monitor()
        with patch.object(windows, "open_monitor") as monitor:
            windows.start()
        monitor.assert_called_once()
        self.assertEqual(mine(self.dir)["role"], instances.ROLE_STANDALONE)

    def test_the_host_view_opens_the_host_monitor(self):
        windows = self.monitor()
        with patch.object(windows, "open_host_monitor") as hosts:
            windows.start(host_view=True)
        hosts.assert_called_once()

    def test_opened_by_hand_counts_as_opened(self):
        # Keep running applies only to a session in which the Job Manager was
        # opened; one started by hand plainly was.
        windows = self.monitor()
        with patch.object(windows, "open_monitor"):
            windows.start()
        self.assertTrue(presence.current().tray.opened)

    def test_its_tray_menu_reaches_its_own_windows(self):
        # The plugin's menu asks a MoleditPy that is not there.
        windows = self.monitor()
        with patch.object(windows, "open_monitor"):
            windows.start()
        tray = presence.current().tray
        self.assertFalse(tray.standalone)
        self.assertEqual(tray.quit_label, "Quit Job Manager")
        self.assertEqual(tray.actions["host_monitor"], windows.open_host_monitor)
        self.assertEqual(tray.actions["settings"], windows.open_settings)

    def test_the_tray_raises_the_host_monitor_the_job_monitor_opened(self):
        # No window registry here: each route used to open a window of its own.
        windows = self.monitor()
        windows.start()
        windows.monitor.open_host_monitor()
        opened = windows.monitor._host_monitor
        self.addCleanup(opened.close)
        self.assertIs(windows.open_host_monitor(), opened)

    def test_and_the_job_monitor_raises_the_trays(self):
        windows = self.monitor()
        windows.start()
        opened = windows.open_host_monitor()
        self.addCleanup(opened.close)
        with patch("job_manager.host_monitor.HostMonitorDialog") as dialog_cls:
            windows.monitor.open_host_monitor()
        dialog_cls.assert_not_called()

    def test_closing_it_hands_over_without_a_way_back_to_moleditpy(self):
        # "Open MoleditPy" in the tray process would otherwise start another
        # standalone monitor.
        from job_manager import handoff

        main = os.path.join(os.path.dirname(handoff.__file__), "__main__.py")
        self.assertEqual(handoff.relaunch_command([main], "python"), [])


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
    def main(self, *args, running=None):
        from job_manager import __main__ as entry

        with (
            patch.object(sys, "argv", ["__main__.py", *args]),
            patch("job_manager.standalone.run", return_value=0) as tray_run,
            patch("job_manager.standalone.run_monitor", return_value=0) as monitor_run,
            patch("job_manager.instances.defer_to_running", return_value=running) as defer,
            patch("job_manager.handoff.stop_running_tray") as legacy,
            patch.object(self.app, "exec", return_value=0),
        ):
            code = entry.main()
        return code, tray_run, monitor_run, defer, legacy

    def test_tray_mode_carries_the_way_back_and_who_handed_over(self):
        code, tray_run, _, defer, _ = self.main(
            "--tray", "--relaunch", '["moleditpy", "--x"]', "--after-pid", "77"
        )
        self.assertEqual(tray_run.call_args[0][3], ["moleditpy", "--x"])
        self.assertEqual(tray_run.call_args[0][4], 77)
        # The tray process decides for itself; it never defers by request.
        defer.assert_not_called()

    def test_a_tray_process_with_nothing_to_do_builds_nothing(self):
        # Exits before the service, so no job list is read and no tray icon
        # goes up and comes down again.
        with patch("job_manager.instances.standalone_running", return_value=True) as running:
            with patch("job_manager.__main__.get_service") as service:
                code, tray_run, _, _, _ = self.main("--tray", "--after-pid", "77")
        self.assertEqual(code, 0)
        self.assertEqual(running.call_args[0][1], 77)
        service.assert_not_called()
        tray_run.assert_not_called()

    def test_garbled_arguments_are_dropped(self):
        _, tray_run, _, _, _ = self.main("--tray", "--relaunch", "not json", "--after-pid", "x")
        self.assertEqual(tray_run.call_args[0][3], [])
        self.assertEqual(tray_run.call_args[0][4], 0)

    def test_opening_it_by_hand_alone_starts_a_monitor(self):
        code, tray_run, monitor_run, defer, legacy = self.main()
        tray_run.assert_not_called()
        monitor_run.assert_called_once()
        self.assertEqual(defer.call_args[0][1], instances.ACTION_SHOW_MONITOR)
        legacy.assert_called_once()

    def test_opening_it_beside_a_running_one_brings_that_up_instead(self):
        code, tray_run, monitor_run, defer, _ = self.main(
            running={"pid": 4242, "role": instances.ROLE_TRAY}
        )
        self.assertEqual(code, 0)
        monitor_run.assert_not_called()
        tray_run.assert_not_called()

    def test_the_host_monitor_flag_asks_for_the_host_monitor(self):
        _, _, monitor_run, defer, _ = self.main("--host-monitor")
        self.assertEqual(defer.call_args[0][1], instances.ACTION_SHOW_HOST_MONITOR)
        self.assertTrue(monitor_run.call_args[0][3])


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

    def test_a_registered_tray_process_is_asked_to_stop_and_waited_for(self):
        fake_instance(self.dir, 4242, instances.ROLE_TRAY)
        fake_instance(self.dir, 4343, instances.ROLE_MOLEDITPY)
        with (
            patch("job_manager.instances.send_request") as send,
            patch("job_manager.instances.wait_until_gone", return_value=True) as wait,
            patch("job_manager.handoff.stop_running_tray"),
        ):
            job_manager._take_tracking_back()
        send.assert_called_once_with(self.dir, 4242, instances.ACTION_STOP)
        wait.assert_called_once_with(self.dir, 4242)

    def test_taking_over_says_so(self):
        fake_instance(self.dir, 4242, instances.ROLE_TRAY)
        with (
            patch("job_manager.instances.send_request"),
            patch("job_manager.instances.wait_until_gone", return_value=True),
            patch("job_manager.handoff.stop_running_tray"),
        ):
            self.assertTrue(job_manager._take_tracking_back())

    def test_with_nothing_running_there_is_nothing_taken_over(self):
        fake_instance(self.dir, 4343, instances.ROLE_MOLEDITPY)
        self.assertFalse(job_manager._take_tracking_back())

    def test_an_old_tray_process_counts_as_taken_over(self):
        with (
            patch("job_manager.handoff.live_tray", return_value={"beat": 0}),
            patch("job_manager.handoff.stop_running_tray"),
        ):
            self.assertTrue(job_manager._take_tracking_back())

    def test_a_taken_over_job_manager_is_kept_running_after_moleditpy(self):
        # It was running before this MoleditPy started, so closing MoleditPy
        # must hand it back to a tray process -- not end it because nothing
        # was opened in this session.
        context = MagicMock()
        context.get_window.return_value = None
        with (
            patch.object(job_manager, "_take_tracking_back", return_value=True),
            patch.object(job_manager, "_mark_opened") as opened,
        ):
            job_manager.initialize(context)
        self.assertIsNotNone(job_manager.get_service(create=False))
        opened.assert_called_once()

    def test_nothing_taken_over_leaves_it_unopened(self):
        context = MagicMock()
        context.get_window.return_value = None
        with (
            patch.object(job_manager, "_take_tracking_back", return_value=False),
            patch.object(job_manager, "_mark_opened") as opened,
        ):
            job_manager.initialize(context)
        self.assertIsNone(job_manager.get_service(create=False))
        opened.assert_not_called()

    def test_a_plugin_with_a_service_registers_and_answers(self):
        context = MagicMock()
        context.get_window.return_value = None
        job_manager.initialize(context)
        self.assertIsNone(mine(self.dir))  # nothing opened, nothing tracked yet
        job_manager.get_service()
        self.assertEqual(mine(self.dir)["role"], instances.ROLE_MOLEDITPY)

        with patch.object(job_manager, "show_monitor") as show:
            instances.send_request(self.dir, os.getpid(), instances.ACTION_SHOW_MONITOR)
            job_manager._beacon.check_requests()
        show.assert_called_once_with(context)
        with patch.object(job_manager, "show_host_monitor_standalone") as hosts:
            instances.send_request(self.dir, os.getpid(), instances.ACTION_SHOW_HOST_MONITOR)
            job_manager._beacon.check_requests()
        hosts.assert_called_once_with(context)

        job_manager.shutdown()
        self.assertIsNone(mine(self.dir))

    def test_a_plugin_ignores_a_stop_request(self):
        # Only the tray process steps aside; a MoleditPy is never stopped from
        # outside.
        context = MagicMock()
        context.get_window.return_value = None
        job_manager.initialize(context)
        job_manager.get_service()
        self.assertNotIn(instances.ACTION_STOP, job_manager._beacon.handlers)

    def test_the_service_without_a_plugin_registers_nothing_itself(self):
        # A standalone monitor or the tray process registers in its own role,
        # and installs its own tray menu: the plugin's would ask a context
        # that is not there.
        job_manager._context = None
        job_manager.get_service()
        self.assertIsNone(mine(self.dir))
        self.assertIsNone(presence.current())

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
