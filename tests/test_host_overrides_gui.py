"""Per-host overrides where they take effect: the poller, the Host Monitor,
and the two dialogs that edit them.

The case they exist for is a supercomputer's login node: shared by hundreds,
so its load is not worth sampling, and its queue is worth asking less often
than a workstation's.
"""

from __future__ import annotations

import time
import unittest

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager.models import STATE_PENDING, Job  # noqa: E402

from .fakes import make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402
from .test_host_monitor_gui import HostMonitorTestCase  # noqa: E402


class TestThePollerHonoursAHostInterval(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.set_pref("poll_interval", 120)
        self.poller = self.service.poller

    def add_job(self, host_id, job_id):
        self.store.add_job(
            Job(id=job_id, host_id=host_id, remote_job_id="1", remote_dir=f"/j/{job_id}")
        )
        self.store.jobs[job_id].state = STATE_PENDING

    def test_the_timer_runs_as_often_as_the_most_eager_host(self):
        fast = make_host(id="fast", poll_interval=30)
        self.store.add_host(fast)
        self.add_job(self.host.id, "a")
        self.add_job(fast.id, "b")
        self.assertEqual(self.poller.interval_ms(), 30_000)

    def test_a_slower_host_does_not_slow_the_timer(self):
        self.host.poll_interval = 900
        self.add_job(self.host.id, "a")
        other = make_host(id="other")
        self.store.add_host(other)
        self.add_job(other.id, "b")
        self.assertEqual(self.poller.interval_ms(), 120_000)

    def test_a_host_without_active_jobs_does_not_count(self):
        self.store.add_host(make_host(id="idle", poll_interval=10))
        self.add_job(self.host.id, "a")
        self.assertEqual(self.poller.interval_ms(), 120_000)

    def test_after_a_poll_the_host_is_next_asked_on_its_own_interval(self):
        self.host.poll_interval = 900
        self.add_job(self.host.id, "a")
        before = time.time()
        self.poller._on_poll_finished(self.host.id, {}, {}, {})
        wait = self.poller._next_poll[self.host.id] - before
        # +/-10% jitter.
        self.assertGreater(wait, 800)
        self.assertLess(wait, 1000)

    def test_a_failure_backs_off_from_the_host_interval(self):
        self.host.poll_interval = 600
        self.add_job(self.host.id, "a")
        self.poller._on_poll_failed(self.host.id, "down")
        self.assertEqual(self.poller.backoff_for(self.host.id), 900.0)


class TestAHostThatIsNotSampled(HostMonitorTestCase):
    def setUp(self):
        super().setUp()
        self.login = make_host(id="login", name="supercomputer", monitor_usage=False)
        self.store.add_host(self.login)

    def test_it_is_never_probed(self):
        dialog = self.monitor()
        for _ in range(3):
            dialog._sample_all()
        self.assertNotIn("login", self.transports)
        self.assertNotIn("login", [host.id for host in dialog._hosts()])

    def test_it_keeps_a_card_that_says_so(self):
        from job_manager.host_monitor import NOT_SAMPLED

        dialog = self.monitor()
        self.assertIn("login", dialog.cards)
        self.assertEqual(dialog.cards["login"].lbl_state.text(), NOT_SAMPLED)

    def test_its_jobs_are_still_shown(self):
        self.store.add_job(Job(id="j1", name="mol", host_id="login", state="RUNNING"))
        dialog = self.monitor()
        dialog._refresh_card_jobs()
        line = dialog.cards["login"].lbl_job
        self.assertIn("mol", line._head + line._tail)

    def test_the_web_view_says_so_and_serves_no_numbers(self):
        from job_manager import host_stats
        from job_manager.host_monitor import NOT_SAMPLED

        dialog = self.monitor()
        dialog._latest["login"] = host_stats.parse(
            "cores=8\nload=4.0 4.0 4.0\nmem_total=64000\nmem_free=100\n"
        )
        entry = [e for e in dialog._web_snapshot()["hosts"] if e["name"] == "supercomputer"][0]
        self.assertEqual(entry["summary"], NOT_SAMPLED)
        self.assertEqual(entry["load_fraction"], 0.0)

    def test_switching_it_off_rebuilds_the_cards(self):
        dialog = self.monitor()
        self.host.monitor_usage = False
        self.store.add_host(self.host)
        dialog._sample_all()
        self.assertNotIn(self.host.id, [host.id for host in dialog._hosts()])

    def test_the_other_hosts_are_still_sampled(self):
        self.monitor()
        self.assertGreaterEqual(self.transports[self.host.id].runs, 1)


class TestAHostWithItsOwnMonitorInterval(HostMonitorTestCase):
    def test_a_slower_host_is_skipped_until_its_time(self):
        slow = make_host(id="slow", monitor_interval=600)
        self.store.add_host(slow)
        dialog = self.monitor()
        runs = self.transports["slow"].runs
        dialog._sample_all()
        dialog._sample_all()
        self.assertEqual(self.transports["slow"].runs, runs)
        # The window's own host is asked every tick, as before.
        self.assertGreaterEqual(self.transports[self.host.id].runs, 3)

    def test_it_is_asked_once_its_interval_has_passed(self):
        slow = make_host(id="slow", monitor_interval=600)
        self.store.add_host(slow)
        dialog = self.monitor()
        runs = self.transports["slow"].runs
        dialog._last_sample["slow"] -= 601
        dialog._sample_all()
        self.assertEqual(self.transports["slow"].runs, runs + 1)

    def test_a_faster_host_speeds_up_the_timer(self):
        dialog = self.monitor()
        dialog.spin_interval.setValue(30)
        self.store.add_host(make_host(id="quick", monitor_interval=5))
        dialog._sample_all()
        self.assertEqual(dialog._timer.interval(), 5_000)

    def test_a_host_that_is_not_sampled_does_not_set_the_pace(self):
        dialog = self.monitor()
        dialog.spin_interval.setValue(30)
        self.store.add_host(make_host(id="off", monitor_usage=False, monitor_interval=1))
        dialog._sample_all()
        self.assertEqual(dialog._timer.interval(), 30_000)

    def test_no_overrides_is_the_window_setting(self):
        dialog = self.monitor()
        dialog.spin_interval.setValue(17)
        self.assertEqual(dialog._timer.interval(), 17_000)


class TestTheHostsDialogEditsThem(DialogTestCase):
    def setUp(self):
        super().setUp()
        from job_manager.hosts_dialog import HostsDialog

        self.dialog = HostsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_they_are_saved(self):
        from job_manager.store import JobStore

        self.dialog.txt_submit_options.setText("-W group_list=gr1")
        self.dialog.chk_monitor_usage.setChecked(False)
        self.dialog.spin_monitor_interval.setValue(60)
        self.dialog.spin_poll_interval.setValue(600)
        self.dialog._save_current()
        saved = JobStore(self.tmp).hosts[self.host.id]
        self.assertEqual(saved.submit_options, "-W group_list=gr1")
        self.assertFalse(saved.monitor_usage)
        self.assertEqual(saved.monitor_interval, 60)
        self.assertEqual(saved.poll_interval, 600)

    def test_they_are_loaded(self):
        self.host.submit_options = "-A proj"
        self.host.monitor_usage = False
        self.host.poll_interval = 300
        self.store.add_host(self.host)
        self.dialog._reload_list_now(select_id=self.host.id)
        self.dialog._load_selected()
        self.assertEqual(self.dialog.txt_submit_options.text(), "-A proj")
        self.assertFalse(self.dialog.chk_monitor_usage.isChecked())
        self.assertFalse(self.dialog.spin_monitor_interval.isEnabled())
        self.assertEqual(self.dialog.spin_poll_interval.value(), 300)

    def test_a_poll_interval_below_the_floor_is_stored_as_the_floor(self):
        from job_manager.store import MIN_POLL_INTERVAL

        self.dialog.spin_poll_interval.setValue(1)
        self.dialog._save_current()
        self.assertEqual(self.store.hosts[self.host.id].poll_interval, MIN_POLL_INTERVAL)

    def test_zero_means_follow_the_global(self):
        self.dialog.spin_poll_interval.setValue(0)
        self.dialog._save_current()
        self.assertEqual(self.store.hosts[self.host.id].poll_interval, 0)
        self.assertEqual(self.dialog.spin_poll_interval.text(), "global setting")

    def test_saving_reschedules_the_poller(self):
        calls = []
        self.service.poller.reschedule = lambda: calls.append(1)
        self.dialog._save_current()
        self.assertEqual(calls, [1])


class TestTheSubmitDialogShowsTheSubmitLine(DialogTestCase):
    def setUp(self):
        super().setUp()
        from job_manager.submit_dialog import SubmitDialog

        self.host.submit_options = "-W group_list=gr1"
        self.store.add_host(self.host)
        self.dialog = SubmitDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_the_preview_shows_both_sets_of_options(self):
        self.dialog.spin_cpus.setEnabled(True)
        self.dialog.chk_scan_resources.setChecked(False)
        self.dialog.spin_cpus.setValue(4)
        self.dialog.txt_submit_options.setText("--qos=long --cpus={cpus}")
        self.dialog._refresh_preview()
        line = self.dialog.lbl_submit_line.text()
        self.assertIn("sbatch --parsable -W group_list=gr1 --qos=long --cpus=4", line)

    def test_a_stray_quote_is_reported_in_the_preview(self):
        self.dialog.txt_submit_options.setText('-N "oops')
        self.dialog._refresh_preview()
        self.assertIn("could not be read", self.dialog.lbl_submit_line.text())

    def test_the_preset_carries_them(self):
        self.dialog.txt_submit_options.setText("--qos=long")
        self.assertEqual(self.dialog.collect_preset().submit_options, "--qos=long")

    def test_a_host_with_no_queue_shows_no_submit_line(self):
        self.host.scheduler = "shell"
        self.store.add_host(self.host)
        self.dialog._reload_hosts()
        self.dialog._refresh_preview()
        self.assertEqual(self.dialog.lbl_submit_line.text(), "")
        self.assertFalse(self.dialog.txt_submit_options.isEnabled())


if __name__ == "__main__":
    unittest.main()
