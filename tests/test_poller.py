"""Poller behaviour: one call per host, backoff, in-flight guard, stop/start.

Tasks are run synchronously through a fake pool, so the scheduling logic is
tested without threads or an event loop; signal delivery is direct because
everything happens on one thread.
"""

import tempfile
import time
import unittest

import pytest

pytest.importorskip("PyQt6.QtCore", reason="PyQt6 is not installed")

from job_manager.models import (  # noqa: E402
    STATE_DONE,
    STATE_PENDING,
    STATE_RUNNING,
    Job,
)
from job_manager.poller import MANUAL_REFRESH_COOLDOWN, MAX_BACKOFF, JobPoller  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from job_manager.transport.base import TransportError  # noqa: E402

from .fakes import FakeTransport, make_host  # noqa: E402


class SyncPool:
    """Runs every queued task inline."""

    def __init__(self):
        self.started = 0

    def setMaxThreadCount(self, count):
        pass

    def start(self, task):
        self.started += 1
        task.run_sync()

    def clear(self):
        pass

    def waitForDone(self, msecs=0):
        return True


class PollerTestCase(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(tempfile.mkdtemp(prefix="poller_"))
        self.host = make_host()
        self.store.add_host(self.host)
        self.transport = FakeTransport(self.host)
        self.poller = JobPoller(self.store, lambda host: self.transport)
        self.poller.pool = SyncPool()
        self.addCleanup(self.poller.stop)

    def add_job(self, job_id, state=STATE_PENDING, queue_id="100", host_id=None):
        job = Job(
            id=job_id,
            host_id=host_id or self.host.id,
            remote_job_id=queue_id,
            remote_dir=f"/jobs/{job_id}",
            state=state,
        )
        self.store.add_job(job)
        return job


class TestTimerLifecycle(PollerTestCase):
    def test_does_not_start_with_no_active_jobs(self):
        self.poller.start()
        self.assertFalse(self.poller.timer.isActive())

    def test_starts_when_a_job_is_active(self):
        self.add_job("j1")
        self.poller.start()
        self.assertTrue(self.poller.timer.isActive())

    def test_interval_comes_from_the_store(self):
        self.store.set_pref("poll_interval", 300)
        self.add_job("j1")
        self.poller.start()
        self.assertEqual(self.poller.timer.interval(), 300_000)

    def test_interval_is_floored_even_if_the_pref_is_absurd(self):
        self.store.set_pref("poll_interval", 0)
        self.assertEqual(self.poller.interval_ms(), 5_000)

    def test_a_fast_interval_is_honoured(self):
        self.store.set_pref("poll_interval", 10)
        self.assertEqual(self.poller.interval_ms(), 10_000)

    def test_reschedule_applies_a_new_interval(self):
        self.add_job("j1")
        self.poller.start()
        self.store.set_pref("poll_interval", 600)
        self.poller.reschedule()
        self.assertEqual(self.poller.timer.interval(), 600_000)

    def test_timer_stops_once_everything_is_terminal(self):
        job = self.add_job("j1", state=STATE_RUNNING)
        self.poller.start()
        self.transport.when("squeue", stdout="")
        self.transport.when(".moleditpy_rc", stdout="@@MOLEDITPY@@\n0\n")
        self.poller.tick(force=True)
        self.assertEqual(job.state, STATE_DONE)
        self.assertFalse(self.poller.timer.isActive())

    def test_tick_with_no_jobs_stops_the_timer(self):
        self.add_job("j1")
        self.poller.start()
        self.store.remove_job("j1")
        self.assertEqual(self.poller.tick(), 0)
        self.assertFalse(self.poller.timer.isActive())

    def test_shutdown_stops_the_timer(self):
        self.add_job("j1")
        self.poller.start()
        self.poller.shutdown()
        self.assertFalse(self.poller.timer.isActive())


class TestPollDispatch(PollerTestCase):
    def test_one_task_per_host_not_per_job(self):
        for index in range(4):
            self.add_job(f"j{index}", queue_id=str(100 + index))
        self.transport.when("squeue", stdout="")
        self.transport.when(".moleditpy_rc", stdout="@@MOLEDITPY@@\n0\n" * 4)
        self.assertEqual(self.poller.tick(force=True), 1)

    def test_two_hosts_get_two_tasks(self):
        other = make_host(id="host2", name="second")
        self.store.add_host(other)
        self.add_job("j1")
        self.add_job("j2", host_id="host2")
        self.assertEqual(self.poller.tick(force=True), 2)

    def test_a_job_whose_host_vanished_is_skipped(self):
        self.add_job("j1", host_id="ghost")
        self.assertEqual(self.poller.tick(force=True), 0)

    def test_state_updates_are_applied_and_persisted(self):
        job = self.add_job("j1", state=STATE_PENDING)
        self.transport.when("squeue", stdout="100 RUNNING\n")
        self.poller.tick(force=True)
        self.assertEqual(job.state, STATE_RUNNING)
        self.assertEqual(JobStore(self.store.directory).jobs["j1"].state, STATE_RUNNING)

    def test_job_updated_signal_fires(self):
        self.add_job("j1", state=STATE_PENDING)
        seen = []
        self.poller.job_updated.connect(lambda job_id, state: seen.append((job_id, state)))
        self.transport.when("squeue", stdout="100 RUNNING\n")
        self.poller.tick(force=True)
        self.assertEqual(seen, [("j1", STATE_RUNNING)])

    def test_no_signal_when_nothing_changed(self):
        self.add_job("j1", state=STATE_RUNNING)
        seen = []
        self.poller.job_updated.connect(lambda *args: seen.append(args))
        self.transport.when("squeue", stdout="100 RUNNING\n")
        self.poller.tick(force=True)
        self.assertEqual(seen, [])

    def test_not_due_hosts_are_skipped(self):
        self.add_job("j1")
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.poller.tick(force=True)
        before = self.transport.count_matching("squeue")
        self.poller.tick()  # not forced, and the next slot is in the future
        self.assertEqual(self.transport.count_matching("squeue"), before)

    def test_next_poll_is_scheduled_after_success(self):
        self.add_job("j1")
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.poller.tick(force=True)
        self.assertGreater(self.poller._next_poll[self.host.id], time.time())


class TestErrorHandling(PollerTestCase):
    def setUp(self):
        super().setUp()
        self.add_job("j1")

        def failing_factory(host):
            raise TransportError("network unreachable")

        self.poller.transport_factory = failing_factory

    def test_error_is_reported_not_raised(self):
        errors = []
        self.poller.host_error.connect(lambda host_id, msg: errors.append(msg))
        self.poller.tick(force=True)
        self.assertEqual(len(errors), 1)
        self.assertIn("network unreachable", errors[0])

    def test_backoff_grows(self):
        self.poller.tick(force=True)
        first = self.poller.backoff_for(self.host.id)
        self.poller._next_poll.clear()
        self.poller.tick(force=True)
        self.assertGreater(self.poller.backoff_for(self.host.id), first)

    def test_backoff_is_capped(self):
        for _ in range(20):
            self.poller._next_poll.clear()
            self.poller.tick(force=True)
        self.assertLessEqual(self.poller.backoff_for(self.host.id), MAX_BACKOFF)

    def test_backoff_clears_after_a_success(self):
        self.poller.tick(force=True)
        self.assertGreater(self.poller.backoff_for(self.host.id), 0)
        self.poller.transport_factory = lambda host: self.transport
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.poller._next_poll.clear()
        self.poller.tick(force=True)
        self.assertEqual(self.poller.backoff_for(self.host.id), 0.0)

    def test_the_host_is_released_after_a_failure(self):
        self.poller.tick(force=True)
        self.assertFalse(self.poller.is_in_flight(self.host.id))

    def test_an_unexpected_exception_is_also_contained(self):
        def exploding_factory(host):
            raise ZeroDivisionError("bug")

        self.poller.transport_factory = exploding_factory
        errors = []
        self.poller.host_error.connect(lambda host_id, msg: errors.append(msg))
        self.poller.tick(force=True)
        self.assertEqual(len(errors), 1)


class TestInFlightGuard(PollerTestCase):
    def test_a_host_already_polling_is_not_polled_again(self):
        self.add_job("j1")
        self.poller._in_flight[self.host.id] = True
        self.assertEqual(self.poller.tick(force=True), 0)

    def test_the_flag_clears_after_a_successful_poll(self):
        self.add_job("j1")
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.poller.tick(force=True)
        self.assertFalse(self.poller.is_in_flight(self.host.id))


class TestManualRefresh(PollerTestCase):
    def test_first_refresh_runs(self):
        self.add_job("j1")
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.assertTrue(self.poller.refresh_now())

    def test_second_refresh_is_rate_limited(self):
        self.add_job("j1")
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.poller.refresh_now()
        self.assertFalse(self.poller.refresh_now())

    def test_refresh_is_allowed_again_after_the_cooldown(self):
        self.add_job("j1")
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.poller.refresh_now()
        self.poller._last_manual -= MANUAL_REFRESH_COOLDOWN + 1
        self.assertTrue(self.poller.refresh_now())

    def test_refresh_clears_any_backoff(self):
        self.add_job("j1")
        self.poller._backoff[self.host.id] = 900
        self.transport.when("squeue", stdout="100 PENDING\n")
        self.poller.refresh_now()
        self.assertEqual(self.poller.backoff_for(self.host.id), 0.0)


class TestJitter(PollerTestCase):
    def test_next_poll_stays_close_to_the_interval(self):
        self.add_job("j1")
        interval = float(self.store.poll_interval)
        self.poller._schedule_next(self.host.id, interval)
        delay = self.poller._next_poll[self.host.id] - time.time()
        self.assertGreater(delay, interval * 0.85)
        self.assertLess(delay, interval * 1.15)

    def test_a_tiny_delay_never_becomes_a_busy_loop(self):
        self.poller._schedule_next(self.host.id, 0.0)
        self.assertGreaterEqual(self.poller._next_poll[self.host.id] - time.time(), 0.9)


if __name__ == "__main__":
    unittest.main()
