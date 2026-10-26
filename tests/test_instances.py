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


def fake_instance(directory, server, pid, role, started=None):
    with patch("job_manager.instances.os.getpid", return_value=pid):
        instances.register_instance(directory, role, started or time.time(), server)


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="instances_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.server = instances.LivenessServer()
        self.addCleanup(self.server.close)


class TestTheRegistry(RegistryTestCase):
    def test_nobody_is_nobody(self):
        self.assertEqual(instances.live_instances(self.dir), [])

    def test_this_process_never_counts_itself(self):
        instances.register_instance(self.dir, instances.ROLE_STANDALONE, time.time(), self.server)
        self.assertEqual(instances.live_instances(self.dir), [])

    def test_others_are_listed_newest_first(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_MOLEDITPY, started=100.0)
        fake_instance(self.dir, self.server, 102, instances.ROLE_MOLEDITPY, started=200.0)
        pids = [data["pid"] for data in instances.live_instances(self.dir)]
        self.assertEqual(pids, [102, 101])

    def test_a_closed_socket_is_a_dead_process_and_is_cleared(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_TRAY)
        self.server.close()
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
        instances.register_instance(self.dir, instances.ROLE_TRAY, time.time(), self.server)
        instances.send_request(self.dir, os.getpid(), instances.ACTION_STOP)
        instances.unregister_instance(self.dir)
        self.assertEqual(os.listdir(instances.instances_dir(self.dir)), [])
        instances.unregister_instance(self.dir)  # twice is fine

    def test_a_refused_replace_is_retried_once(self):
        # Windows refuses to replace a file another process is reading.
        real = os.replace
        calls = []

        def busy_once(src, dst):
            calls.append(dst)
            if len(calls) == 1:
                raise PermissionError("in use")
            return real(src, dst)

        with patch("job_manager.instances.os.replace", side_effect=busy_once):
            instances.register_instance(self.dir, instances.ROLE_TRAY, time.time(), self.server)
        self.assertEqual(len(calls), 2)
        self.assertEqual(os.listdir(instances.instances_dir(self.dir)), [f"{os.getpid()}.json"])

    def test_a_write_that_fails_leaves_no_temp_file(self):
        with patch("job_manager.instances.os.replace", side_effect=PermissionError("in use")):
            with self.assertRaises(PermissionError):
                instances.register_instance(self.dir, instances.ROLE_TRAY, time.time(), self.server)
        self.assertEqual(os.listdir(instances.instances_dir(self.dir)), [])

    def test_no_temporary_file_is_left(self):
        instances.register_instance(self.dir, instances.ROLE_TRAY, time.time(), self.server)
        self.assertEqual(os.listdir(instances.instances_dir(self.dir)), [f"{os.getpid()}.json"])

    def test_registration_has_no_expiration_or_heartbeat(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_TRAY, started=1.0)
        path = os.path.join(instances.instances_dir(self.dir), "101.json")
        before = os.stat(path).st_mtime_ns
        with patch("job_manager.instances.time.time", return_value=10**12):
            self.assertEqual(len(instances.live_instances(self.dir)), 1)
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        with open(path, encoding="utf-8") as handle:
            self.assertNotIn("beat", json.load(handle))

    def test_reused_port_with_different_token_is_not_alive(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_TRAY)
        path = os.path.join(instances.instances_dir(self.dir), "101.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["token"] = "0" * 64
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        self.assertEqual(instances.live_instances(self.dir), [])

    def test_old_heartbeat_protocol_is_ignored(self):
        path = os.path.join(instances.instances_dir(self.dir), "101.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": 101, "role": "tray", "beat": time.time()}, handle)
        self.assertEqual(instances.live_instances(self.dir), [])

    def test_probe_timeout_keeps_uncertain_instance(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_TRAY)
        with patch("job_manager.instances.socket.create_connection", side_effect=TimeoutError):
            self.assertEqual(len(instances.live_instances(self.dir)), 1)

    def test_invalid_socket_metadata_is_rejected(self):
        for port, token in [
            (True, self.server.token),
            (0, self.server.token),
            (65536, self.server.token),
            (self.server.port, "x" * 64),
            (self.server.port, "short"),
        ]:
            self.assertFalse(instances._socket_alive({"port": port, "token": token}))


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


class TestIsAStandaloneRunning(RegistryTestCase):
    def test_nothing_running(self):
        self.assertFalse(instances.standalone_running(self.dir))

    def test_a_tray_process_or_monitor_counts(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_TRAY)
        self.assertTrue(instances.standalone_running(self.dir))

    def test_a_moleditpy_does_not(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_MOLEDITPY)
        self.assertFalse(instances.standalone_running(self.dir))

    def test_the_one_handing_over_does_not(self):
        fake_instance(self.dir, self.server, 101, instances.ROLE_STANDALONE)
        self.assertFalse(instances.standalone_running(self.dir, ignore_pid=101))
        self.assertTrue(instances.standalone_running(self.dir, ignore_pid=999))


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
        fake_instance(self.dir, self.server, 4242, instances.ROLE_TRAY)
        started = time.monotonic()
        self.assertIsNone(
            instances.defer_to_running(self.dir, instances.ACTION_SHOW_MONITOR, timeout=0.3)
        )
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(instances.request_pending(self.dir, 4242))

    def test_waiting_for_one_to_go(self):
        fake_instance(self.dir, self.server, 4242, instances.ROLE_TRAY)
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
    from PySide6.QtCore import QCoreApplication, QTimer
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
            import PySide6.QtCore  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 is not installed")

    @staticmethod
    def _end(process):
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def start_answerer(self):
        marker = os.path.join(self.dir, "shown.txt")
        code = _ANSWERER.format(
            parent=os.path.dirname(PACKAGE_DIR),
            package=os.path.basename(PACKAGE_DIR),
            marker=marker,
            directory=self.dir,
        )
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        # Nothing of this process's own standard streams: under pytest-xdist
        # they are the pipes to the controller, and a child holding them can
        # keep a worker -- and so the whole run -- from ever finishing.
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._end, process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not instances.live_instances(self.dir):
            self.assertIsNone(process.poll(), "the stand-in exited on its own")
            time.sleep(0.05)
        self.assertTrue(instances.live_instances(self.dir), "no registration within 30 s")
        return process, marker

    def test_idle_beacon_never_rewrites_registration(self):
        process, _ = self.start_answerer()
        path = os.path.join(instances.instances_dir(self.dir), f"{process.pid}.json")
        before = os.stat(path).st_mtime_ns
        time.sleep(2.3)  # Cross the old unconditional two-second rewrite.
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        self.assertEqual([d["pid"] for d in instances.live_instances(self.dir)], [process.pid])

    def test_killed_process_is_detected_without_waiting_for_expiration(self):
        process, _ = self.start_answerer()
        self._end(process)
        self.assertEqual(instances.live_instances(self.dir), [])

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
            stdin=subprocess.DEVNULL,
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
