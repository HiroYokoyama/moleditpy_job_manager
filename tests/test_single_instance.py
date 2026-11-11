"""One standalone tracker per data directory, however close together they start.

Looking in the registry and registering are a second or more apart, so two
launches started together each found nobody and both tracked. Pure stdlib, so
the pytest-only CI job covers it; the last class starts real processes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from job_manager import instances

PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(instances.__file__)))
PACKAGE_NAME = os.path.basename(os.path.dirname(os.path.abspath(instances.__file__)))


class LockTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="single_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def lock(self):
        lock = instances.SingleInstanceLock(self.dir)
        self.addCleanup(lock.release)
        return lock


class TestTheLock(LockTestCase):
    def test_only_one_holder_at_a_time(self):
        first, second = self.lock(), self.lock()
        self.assertTrue(first.acquire())
        self.assertTrue(first.held)
        self.assertFalse(second.acquire())
        self.assertFalse(second.held)

    def test_acquiring_twice_is_harmless(self):
        lock = self.lock()
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.acquire())

    def test_releasing_lets_the_next_one_in(self):
        first, second = self.lock(), self.lock()
        first.acquire()
        first.release()
        self.assertTrue(second.acquire())
        first.release()  # A second release is harmless.

    def test_a_probe_reports_a_holder_and_takes_nothing(self):
        self.assertFalse(instances.standalone_lock_held(self.dir))
        holder = self.lock()
        holder.acquire()
        self.assertTrue(instances.standalone_lock_held(self.dir))
        holder.release()
        self.assertFalse(instances.standalone_lock_held(self.dir))
        self.assertTrue(self.lock().acquire())

    def test_a_probe_does_not_create_the_file(self):
        instances.standalone_lock_held(self.dir)
        self.assertFalse(os.path.exists(instances.instances_dir(self.dir)))

    def test_a_directory_that_cannot_be_written_does_not_block_the_launch(self):
        lock = self.lock()
        with patch("job_manager.instances.os.makedirs", side_effect=OSError("read-only")):
            self.assertTrue(lock.acquire())
        self.assertTrue(lock.unavailable)
        self.assertFalse(lock.held)

    def test_a_killed_holder_leaves_nothing_behind(self):
        code = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {PACKAGE_PARENT!r})
            from {PACKAGE_NAME} import instances
            lock = instances.SingleInstanceLock({self.dir!r})
            assert lock.acquire()
            open({os.path.join(self.dir, "ready")!r}, "w").close()
            time.sleep(60)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not os.path.exists(os.path.join(self.dir, "ready")):
            time.sleep(0.05)
        self.assertTrue(instances.standalone_lock_held(self.dir))
        process.kill()
        process.wait(timeout=10)
        self.assertFalse(instances.standalone_lock_held(self.dir))


class TestALaunchByHand(LockTestCase):
    def test_alone_it_starts_and_keeps_the_lock(self):
        lock, running = instances.claim_or_defer(self.dir, instances.ACTION_SHOW_MONITOR)
        self.addCleanup(lock.release)
        self.assertIsNone(running)
        self.assertTrue(lock.held)

    def test_beside_a_moleditpy_it_defers_and_lets_go_of_the_lock(self):
        target = {"pid": 4242, "role": instances.ROLE_MOLEDITPY}
        with patch("job_manager.instances.defer_to_running", return_value=target):
            lock, running = instances.claim_or_defer(self.dir, instances.ACTION_SHOW_MONITOR)
        self.assertIsNone(lock)
        self.assertEqual(running, target)
        self.assertFalse(instances.standalone_lock_held(self.dir))

    def test_it_waits_for_one_still_starting_and_then_asks_it(self):
        # The first launch holds the lock but has not registered yet: the gap
        # in which a second launch used to start as if alone.
        holder = self.lock()
        holder.acquire()
        registered = {"pid": 4242, "role": instances.ROLE_STANDALONE}
        calls = {"listed": 0}

        def listing(_directory):
            calls["listed"] += 1
            return [registered] if calls["listed"] >= 3 else []

        with (
            patch("job_manager.instances.live_instances", side_effect=listing),
            patch("job_manager.instances.send_request") as sent,
            patch("job_manager.instances.wait_until_taken", return_value=True),
        ):
            lock, running = instances.claim_or_defer(
                self.dir, instances.ACTION_SHOW_HOST_MONITOR, timeout=10, poll=0.01
            )
        self.assertIsNone(lock)
        self.assertEqual(running, registered)
        self.assertEqual(sent.call_args[0][1:], (4242, instances.ACTION_SHOW_HOST_MONITOR))

    def test_it_takes_over_when_the_holder_exits_instead(self):
        holder = self.lock()
        holder.acquire()
        threading.Timer(0.3, holder.release).start()
        lock, running = instances.claim_or_defer(
            self.dir, instances.ACTION_SHOW_MONITOR, timeout=10, poll=0.02
        )
        self.addCleanup(lock.release)
        self.assertIsNone(running)
        self.assertTrue(lock.held)

    def test_a_holder_that_never_answers_is_not_waited_on_for_ever(self):
        holder = self.lock()
        holder.acquire()
        started = time.monotonic()
        lock, running = instances.claim_or_defer(
            self.dir, instances.ACTION_SHOW_MONITOR, timeout=0.3, poll=0.02
        )
        self.assertEqual((lock, running), (None, None))
        self.assertLess(time.monotonic() - started, 5)

    def test_a_registered_one_that_ignores_the_request_gets_it_withdrawn(self):
        holder = self.lock()
        holder.acquire()
        registered = {"pid": 4242, "role": instances.ROLE_TRAY}
        with (
            patch("job_manager.instances.live_instances", return_value=[registered]),
            patch("job_manager.instances.send_request"),
            patch("job_manager.instances.wait_until_taken", return_value=False),
            patch("job_manager.instances.withdraw_request") as withdrawn,
        ):
            result = instances.claim_or_defer(
                self.dir, instances.ACTION_SHOW_MONITOR, timeout=0.1, poll=0.01
            )
        self.assertEqual(result, (None, None))
        withdrawn.assert_called_with(self.dir, 4242)


class TestATrayProcess(LockTestCase):
    def test_alone_it_takes_the_lock(self):
        lock = instances.claim_for_tray(self.dir, 77)
        self.addCleanup(lock.release)
        self.assertTrue(lock.held)

    def test_another_tracker_registered_means_nothing_to_do(self):
        with patch("job_manager.instances.standalone_running", return_value=True) as running:
            self.assertIsNone(instances.claim_for_tray(self.dir, 77))
        self.assertEqual(running.call_args[0], (self.dir, 77))
        self.assertFalse(instances.standalone_lock_held(self.dir))

    def test_it_waits_out_the_monitor_that_is_handing_over(self):
        handing_over = self.lock()
        handing_over.acquire()
        threading.Timer(0.3, handing_over.release).start()
        lock = instances.claim_for_tray(self.dir, 77, timeout=10, poll=0.02)
        self.addCleanup(lock.release)
        self.assertTrue(lock.held)

    def test_a_second_tray_stops_once_the_first_has_registered(self):
        first = self.lock()
        first.acquire()
        answers = iter([False, False, True])
        with patch(
            "job_manager.instances.standalone_running", side_effect=lambda *_: next(answers)
        ):
            self.assertIsNone(instances.claim_for_tray(self.dir, 77, timeout=10, poll=0.01))

    def test_a_holder_that_never_lets_go_does_not_stop_tracking_altogether(self):
        stuck = self.lock()
        stuck.acquire()
        lock = instances.claim_for_tray(self.dir, 77, timeout=0.2, poll=0.02)
        self.assertIsNotNone(lock)
        self.assertFalse(lock.held)


class TestAMoleditPyStartingUp(LockTestCase):
    def test_nothing_starting_costs_no_wait(self):
        started = time.monotonic()
        instances.wait_while_starting(self.dir, timeout=5)
        self.assertLess(time.monotonic() - started, 1)

    def test_it_waits_for_a_tray_process_to_register(self):
        starting = self.lock()
        starting.acquire()
        listed = iter([[], [], [{"pid": 4242, "role": instances.ROLE_TRAY}]])
        with patch("job_manager.instances.live_instances", side_effect=lambda _d: next(listed)):
            instances.wait_while_starting(self.dir, timeout=10, poll=0.01)
        self.assertEqual(list(listed), [])

    def test_the_wait_is_bounded(self):
        starting = self.lock()
        starting.acquire()
        started = time.monotonic()
        instances.wait_while_starting(self.dir, timeout=0.2, poll=0.02)
        self.assertLess(time.monotonic() - started, 3)


#: A launch as ``__main__`` makes it, with the slow part -- building the
#: window and the service before registering -- stood in for by a sleep.
_LAUNCH = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {parent!r})
    from {package} import instances

    import os
    directory, result, others = {directory!r}, {result!r}, {others!r}
    lock, running = instances.claim_or_defer(directory, instances.ACTION_SHOW_MONITOR)
    if running is not None:
        open(result, "w").write("deferred")
        sys.exit(0)
    time.sleep(1.0)
    server = instances.LivenessServer()
    instances.register_instance(directory, instances.ROLE_STANDALONE, time.time(), server)
    open(result, "w").write("tracker")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not all(os.path.exists(path) for path in others):
        instances.take_request(directory)
        time.sleep(0.05)
    instances.unregister_instance(directory)
    server.close()
    if lock is not None:
        lock.release()
    """
)


class TestLaunchesStartedTogether(LockTestCase):
    def test_exactly_one_becomes_the_tracker(self):
        count = 4
        results = [os.path.join(self.dir, f"result{index}.txt") for index in range(count)]
        processes = []
        for result in results:
            code = _LAUNCH.format(
                parent=PACKAGE_PARENT,
                package=PACKAGE_NAME,
                directory=self.dir,
                result=result,
                others=[other for other in results if other != result],
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.addCleanup(process.kill)
            processes.append(process)
        for process in processes:
            self.assertEqual(process.wait(timeout=90), 0)
        outcomes = []
        for result in results:
            with open(result, encoding="utf-8") as handle:
                outcomes.append(handle.read())
        self.assertEqual(sorted(outcomes), ["deferred"] * (count - 1) + ["tracker"])


if __name__ == "__main__":
    unittest.main()
