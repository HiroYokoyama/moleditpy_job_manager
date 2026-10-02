"""The instance registry: who is running, and asking one to show itself.

The first classes are pure stdlib and run in the pytest-only CI job. The last
starts real processes: a Job Manager answering requests, and a standalone
launch beside it that must bring that one up and exit instead of starting a
second tracker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from job_manager import instances

PACKAGE_DIR = os.path.dirname(os.path.abspath(instances.__file__))


def fake_instance(directory, pid, role, beat=None, started=None):
    folder = instances.instances_dir(directory)
    os.makedirs(folder, exist_ok=True)
    now = time.time() if beat is None else beat
    with open(os.path.join(folder, f"{pid}.json"), "w", encoding="utf-8") as handle:
        json.dump({"pid": pid, "role": role, "beat": now, "started": started or now}, handle)


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="instances_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class TestTheRegistry(RegistryTestCase):
    def test_nobody_is_nobody(self):
        self.assertEqual(instances.live_instances(self.dir), [])

    def test_this_process_never_counts_itself(self):
        instances.write_heartbeat(self.dir, instances.ROLE_STANDALONE)
        self.assertEqual(instances.live_instances(self.dir), [])

    def test_others_are_listed_newest_first(self):
        fake_instance(self.dir, 101, instances.ROLE_MOLEDITPY, started=100.0)
        fake_instance(self.dir, 102, instances.ROLE_MOLEDITPY, started=200.0)
        pids = [data["pid"] for data in instances.live_instances(self.dir)]
        self.assertEqual(pids, [102, 101])

    def test_a_stale_heartbeat_is_a_dead_process_and_is_cleared(self):
        old = time.time() - instances.STALE_AFTER_SECONDS - 5
        fake_instance(self.dir, 101, instances.ROLE_TRAY, beat=old)
        with open(os.path.join(instances.instances_dir(self.dir), "101.request"), "w") as handle:
            handle.write("{}")
        self.assertEqual(instances.live_instances(self.dir), [])
        self.assertEqual(os.listdir(instances.instances_dir(self.dir)), [])

    def test_an_unreadable_file_is_skipped_not_deleted(self):
        # It may be a moment from being replaced by a good one.
        folder = instances.instances_dir(self.dir)
        os.makedirs(folder)
        path = os.path.join(folder, "101.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{half")
        self.assertEqual(instances.live_instances(self.dir), [])
        self.assertTrue(os.path.exists(path))

    def test_removing_takes_both_files(self):
        instances.write_heartbeat(self.dir, instances.ROLE_TRAY)
        instances.send_request(self.dir, os.getpid(), instances.ACTION_STOP)
        instances.remove_heartbeat(self.dir)
        self.assertEqual(os.listdir(instances.instances_dir(self.dir)), [])
        instances.remove_heartbeat(self.dir)  # twice is fine

    def test_no_temporary_file_is_left(self):
        instances.write_heartbeat(self.dir, instances.ROLE_TRAY)
        self.assertEqual(os.listdir(instances.instances_dir(self.dir)), [f"{os.getpid()}.json"])


class TestPickingWhomToAsk(unittest.TestCase):
    def test_a_standalone_monitor_comes_first(self):
        found = [
            {"pid": 1, "role": instances.ROLE_MOLEDITPY},
            {"pid": 2, "role": instances.ROLE_TRAY},
            {"pid": 3, "role": instances.ROLE_STANDALONE},
        ]
        self.assertEqual(instances.pick_target(found)["pid"], 3)

    def test_then_the_tray_process(self):
        found = [
            {"pid": 1, "role": instances.ROLE_MOLEDITPY},
            {"pid": 2, "role": instances.ROLE_TRAY},
        ]
        self.assertEqual(instances.pick_target(found)["pid"], 2)

    def test_then_the_newest_moleditpy(self):
        found = [
            {"pid": 5, "role": instances.ROLE_MOLEDITPY},
            {"pid": 1, "role": instances.ROLE_MOLEDITPY},
        ]
        self.assertEqual(instances.pick_target(found)["pid"], 5)

    def test_nobody_to_ask(self):
        self.assertIsNone(instances.pick_target([]))


class TestRequests(RegistryTestCase):
    def test_a_request_is_read_once(self):
        instances.send_request(self.dir, os.getpid(), instances.ACTION_SHOW_MONITOR)
        self.assertTrue(instances.request_pending(self.dir, os.getpid()))
        self.assertEqual(instances.take_request(self.dir), instances.ACTION_SHOW_MONITOR)
        self.assertIsNone(instances.take_request(self.dir))
        self.assertFalse(instances.request_pending(self.dir, os.getpid()))

    def test_a_damaged_request_is_consumed_as_nothing(self):
        os.makedirs(instances.instances_dir(self.dir))
        path = os.path.join(instances.instances_dir(self.dir), f"{os.getpid()}.request")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json")
        self.assertIsNone(instances.take_request(self.dir))
        self.assertFalse(os.path.exists(path))

    def test_nobody_running_means_no_deferring(self):
        self.assertIsNone(instances.defer_to_running(self.dir, instances.ACTION_SHOW_MONITOR))

    def test_one_that_never_answers_is_given_up_on(self):
        # Hung, or on its way out: the launch must not hang with it, nor leave
        # a request behind for whoever reuses that pid.
        fake_instance(self.dir, 4242, instances.ROLE_TRAY)
        started = time.monotonic()
        self.assertIsNone(
            instances.defer_to_running(self.dir, instances.ACTION_SHOW_MONITOR, timeout=0.3)
        )
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(instances.request_pending(self.dir, 4242))

    def test_waiting_for_one_to_go(self):
        fake_instance(self.dir, 4242, instances.ROLE_TRAY)
        self.assertFalse(instances.wait_until_gone(self.dir, 4242, timeout=0.2, poll=0.05))
        os.remove(os.path.join(instances.instances_dir(self.dir), "4242.json"))
        self.assertTrue(instances.wait_until_gone(self.dir, 4242, timeout=0.2, poll=0.05))

    def test_windows_is_asked_to_let_it_come_forward(self):
        with patch.object(sys, "platform", "linux"):
            instances._allow_foreground(1)  # nothing to do, nothing raised
        if sys.platform == "win32":
            instances._allow_foreground(os.getpid())


#: A Job Manager stand-in: the real beacon, answering "show the monitor" by
#: writing a marker file where the test can see it.
_ANSWERER = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {parent!r})
    from PyQt6.QtCore import QCoreApplication, QTimer
    from {package}.beacon import InstanceBeacon
    from {package} import instances

    app = QCoreApplication([])
    marker = {marker!r}

    def shown():
        with open(marker, "w") as handle:
            handle.write("shown")

    beacon = InstanceBeacon({directory!r}, instances.ROLE_TRAY, {{
        instances.ACTION_SHOW_MONITOR: shown,
        instances.ACTION_STOP: app.quit,
    }})
    beacon.start()
    QTimer.singleShot(60000, app.quit)
    app.exec()
    beacon.stop()
    """
)


class TestTwoRealProcesses(RegistryTestCase):
    def setUp(self):
        super().setUp()
        try:
            import PyQt6.QtCore  # noqa: F401
        except ImportError:
            self.skipTest("PyQt6 is not installed")

    def start_answerer(self):
        marker = os.path.join(self.dir, "shown.txt")
        code = _ANSWERER.format(
            parent=os.path.dirname(PACKAGE_DIR),
            package=os.path.basename(PACKAGE_DIR),
            marker=marker,
            directory=self.dir,
        )
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        process = subprocess.Popen([sys.executable, "-c", code], env=env)
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not instances.live_instances(self.dir):
            self.assertIsNone(process.poll(), "the stand-in exited on its own")
            time.sleep(0.05)
        self.assertTrue(instances.live_instances(self.dir), "no heartbeat within 30 s")
        return process, marker

    def test_a_request_reaches_the_running_one(self):
        process, marker = self.start_answerer()
        taken = instances.defer_to_running(self.dir, instances.ACTION_SHOW_MONITOR)
        self.assertEqual(taken["pid"], process.pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not os.path.exists(marker):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(marker))

    def test_a_stop_request_ends_it_and_clears_its_entry(self):
        process, _marker = self.start_answerer()
        instances.send_request(self.dir, process.pid, instances.ACTION_STOP)
        self.assertEqual(process.wait(timeout=15), 0)
        self.assertEqual(instances.live_instances(self.dir), [])

    def test_a_standalone_launch_beside_it_starts_nothing(self):
        # The real entry point, as launch_job_manager.bat runs it: it must find
        # the running one, have it show its window, and exit -- no second
        # tracker, no window of its own.
        process, marker = self.start_answerer()
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MOLEDITPY_JOB_MANAGER_DIR=self.dir)
        launch = subprocess.run(
            [sys.executable, os.path.join(PACKAGE_DIR, "__main__.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(launch.returncode, 0, launch.stderr)
        self.assertIn("already running", launch.stderr)
        self.assertIn(str(process.pid), launch.stderr)
        self.assertTrue(os.path.exists(marker))
        # Only the one that was already there is registered.
        self.assertEqual([d["pid"] for d in instances.live_instances(self.dir)], [process.pid])


if __name__ == "__main__":
    unittest.main()
