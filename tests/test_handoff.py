"""Handing the jobs to a tray process: the files, the commands, the start.

Pure stdlib, so these run in the pytest-only CI job too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from job_manager import handoff


class HandoffTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="handoff_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class TestHeartbeat(HandoffTestCase):
    def test_none_is_none(self):
        self.assertIsNone(handoff.live_tray(self.dir))

    def test_a_fresh_beat_is_alive(self):
        handoff.write_heartbeat(self.dir, ["moleditpy"])
        live = handoff.live_tray(self.dir)
        self.assertEqual(live["pid"], os.getpid())
        self.assertEqual(live["relaunch"], ["moleditpy"])

    def test_a_stale_beat_is_a_dead_process(self):
        # A crashed or killed tray process leaves its file; a MoleditPy must
        # not wait on it, nor believe the jobs are tracked elsewhere.
        handoff.write_heartbeat(self.dir)
        later = time.time() + handoff.STALE_AFTER_SECONDS + 1
        self.assertIsNone(handoff.live_tray(self.dir, now=later))

    def test_a_damaged_file_is_not_alive(self):
        with open(handoff.tray_path(self.dir), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(handoff.live_tray(self.dir))
        with open(handoff.tray_path(self.dir), "w", encoding="utf-8") as handle:
            json.dump(["a list"], handle)
        self.assertIsNone(handoff.live_tray(self.dir))

    def test_no_temporary_file_is_left_behind(self):
        handoff.write_heartbeat(self.dir)
        self.assertEqual(os.listdir(self.dir), [handoff.TRAY_FILE])

    def test_removal_takes_both_files(self):
        handoff.write_heartbeat(self.dir)
        handoff.request_stop(self.dir)
        handoff.remove_tray_file(self.dir)
        self.assertEqual(os.listdir(self.dir), [])
        handoff.remove_tray_file(self.dir)  # twice is fine


class TestStopping(HandoffTestCase):
    def test_nothing_running_returns_at_once(self):
        started = time.monotonic()
        self.assertTrue(handoff.stop_running_tray(self.dir, timeout=5))
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(handoff.stop_requested(self.dir))

    def test_it_waits_for_the_process_to_go(self):
        handoff.write_heartbeat(self.dir)

        def tray_process():
            # What StandaloneTray.tick does: see the request, then leave.
            while not handoff.stop_requested(self.dir):
                time.sleep(0.02)
            time.sleep(0.2)
            os.remove(handoff.tray_path(self.dir))

        worker = threading.Thread(target=tray_process)
        worker.start()
        self.addCleanup(worker.join, 5)

        self.assertTrue(handoff.stop_running_tray(self.dir, timeout=5, poll=0.02))
        self.assertIsNone(handoff.live_tray(self.dir))
        # The request is cleared, or the next tray process would stop at once.
        self.assertFalse(handoff.stop_requested(self.dir))

    def test_one_that_never_goes_is_given_up_on(self):
        handoff.write_heartbeat(self.dir)
        self.assertFalse(handoff.stop_running_tray(self.dir, timeout=0.2, poll=0.05))

    def test_clearing_a_request_that_is_not_there_is_fine(self):
        handoff.clear_stop(self.dir)


class TestCommands(unittest.TestCase):
    def test_it_runs_main_by_path_with_tray(self):
        command = handoff.standalone_command(os.path.join("opt", "plugins", "job-manager"))
        self.assertEqual(command[1], os.path.join("opt", "plugins", "job-manager", "__main__.py"))
        self.assertEqual(command[2:], ["--tray"])

    def test_the_way_back_travels_as_json(self):
        command = handoff.standalone_command("pkg", ["moleditpy", "--x"])
        self.assertEqual(json.loads(command[command.index("--relaunch") + 1]), ["moleditpy", "--x"])

    def test_the_one_handing_over_is_named(self):
        # It is still running, and registered, while the tray process starts;
        # without its pid the tray process would defer to it and exit.
        command = handoff.standalone_command("pkg", after_pid=4242)
        self.assertEqual(command[command.index("--after-pid") + 1], "4242")
        self.assertNotIn("--after-pid", handoff.standalone_command("pkg"))

    def test_windows_uses_pythonw_when_it_is_there(self):
        exe = "C:\\Python\\python.exe"
        with patch("job_manager.handoff.os.path.exists", return_value=True):
            got = handoff.background_python(exe, "win32")
        self.assertTrue(got.lower().endswith("pythonw.exe"))
        with patch("job_manager.handoff.os.path.exists", return_value=False):
            self.assertEqual(handoff.background_python(exe, "win32"), exe)

    def test_elsewhere_the_interpreter_is_kept(self):
        self.assertEqual(handoff.background_python("/usr/bin/python3", "linux"), "/usr/bin/python3")

    def test_a_frozen_moleditpy_cannot_hand_off(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertFalse(handoff.can_hand_off())
        self.assertTrue(handoff.can_hand_off())


class TestTheWayBack(unittest.TestCase):
    def test_a_script(self):
        self.assertEqual(
            handoff.relaunch_command(["/opt/bin/moleditpy", "file.mol"], "/usr/bin/python3"),
            ["/usr/bin/python3", "/opt/bin/moleditpy"],
        )

    def test_a_console_script_launcher(self):
        launcher = "C:\\Python\\Scripts\\moleditpy.exe"
        self.assertEqual(handoff.relaunch_command([launcher], "python.exe"), [launcher])

    def test_python_dash_m(self):
        main = os.path.join("site-packages", "moleditpy", "__main__.py")
        self.assertEqual(handoff.relaunch_command([main], "python"), ["python", "-m", "moleditpy"])

    def test_this_package_run_on_its_own_has_no_moleditpy_to_go_back_to(self):
        own_main = os.path.join(os.path.dirname(os.path.abspath(handoff.__file__)), "__main__.py")
        self.assertEqual(handoff.relaunch_command([own_main], "python"), [])

    def test_nothing_to_go_on(self):
        self.assertEqual(handoff.relaunch_command([], "python"), [])
        self.assertEqual(handoff.relaunch_command(["-c"], "python"), [])

    def test_frozen(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(handoff.relaunch_command(["x"], "/opt/MoleditPy"), ["/opt/MoleditPy"])


class TestSpawning(unittest.TestCase):
    def test_nothing_to_run(self):
        self.assertFalse(handoff.spawn_detached([]))

    def test_a_missing_program_is_false_not_an_exception(self):
        self.assertFalse(
            handoff.spawn_detached([os.path.join(tempfile.gettempdir(), "no-such-program")])
        )

    def test_it_detaches(self):
        with patch("job_manager.handoff.subprocess.Popen") as popen:
            self.assertTrue(handoff.spawn_detached(["prog"], cwd="somewhere"))
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], "somewhere")
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & 0x8)
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_it_really_starts_something_that_outlives_the_call(self):
        marker = os.path.join(tempfile.mkdtemp(prefix="spawn_"), "ran")
        self.addCleanup(shutil.rmtree, os.path.dirname(marker), ignore_errors=True)
        code = f"open({marker!r}, 'w').write('ok')"
        self.assertTrue(handoff.spawn_detached([sys.executable, "-c", code]))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not os.path.exists(marker):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
