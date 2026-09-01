"""The two halves that need Qt: the Reload List button, and what a standalone
launch is wired to when a job ends.

The store-level rules live in ``test_multi_instance_reload.py``, which stays
importable without PyQt6 for the CI job that installs only pytest.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import job_manager  # noqa: E402
from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import STATE_DONE, STATE_RUNNING, Job  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

from .fakes import make_host  # noqa: E402
from .test_poller import SyncPool  # noqa: E402


def make_job(job_id: str, host, **overrides) -> Job:
    fields = dict(
        id=job_id,
        name=job_id,
        host_id=host.id,
        host_name=host.name,
        remote_job_id="42",
        updated_at=1000.0,
    )
    fields.update(overrides)
    return Job(**fields)


class ReloadServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reloadsvc_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(self.tmp)
        self.host = make_host()
        self.store.add_host(self.host)
        self.service = JobService(self.store)
        self.service.pool = SyncPool()
        self.service.poller.pool = SyncPool()
        self.addCleanup(self.service.shutdown)
        #: The other instance, over the same directory.
        self.theirs = JobStore(self.tmp)


class TestTheServiceAnnouncesAReload(ReloadServiceTestCase):
    def test_a_change_reaches_the_table(self):
        seen = []
        self.service.jobs_changed.connect(lambda: seen.append(1))
        self.theirs.add_job(make_job("j1", self.host))
        result = self.service.reload_jobs()
        self.assertEqual(result.added, 1)
        self.assertEqual(len(seen), 1)

    def test_no_change_is_not_announced(self):
        # jobs_changed resets the whole model and drops the selection, so a
        # reload that found nothing must not fire it.
        seen = []
        self.service.jobs_changed.connect(lambda: seen.append(1))
        self.service.reload_jobs()
        self.assertEqual(seen, [])

    def test_a_job_that_arrived_active_starts_the_poller(self):
        # Nothing here has ever asked this host anything; without start() the
        # job would sit at RUNNING for ever with no timer behind it.
        self.assertFalse(self.service.poller.timer.isActive())
        self.theirs.add_job(make_job("j1", self.host, state=STATE_RUNNING))
        self.service.reload_jobs()
        self.assertTrue(self.service.poller.timer.isActive())

    def test_a_reload_that_empties_the_list_stops_the_poller(self):
        self.store.add_job(make_job("j1", self.host, state=STATE_RUNNING))
        self.service.poller.start()
        self.assertTrue(self.service.poller.timer.isActive())
        self.theirs.reload_jobs()
        self.theirs.remove_job("j1")
        self.service.reload_jobs()
        self.assertFalse(self.service.poller.timer.isActive())


class TestTheButton(ReloadServiceTestCase):
    def setUp(self):
        super().setUp()
        self.transport = None
        self.service.transport_for = lambda host: self.transport
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_it_is_not_the_same_button_as_refresh_now(self):
        # Refresh Now asks the hosts; this one reads the file. A job the other
        # instance submitted is not in the list Refresh Now asks about at all.
        self.assertIsNot(self.dialog.btn_reload, self.dialog.btn_refresh)

    def test_clicking_it_brings_in_the_other_instances_job(self):
        self.theirs.add_job(make_job("j1", self.host))
        self.dialog.btn_reload.click()
        self.assertEqual(self.dialog.model.rowCount(), 1)
        self.assertIn("1 new", self.dialog.txt_log.toPlainText())

    def test_it_says_so_when_there_was_nothing_to_take(self):
        self.dialog.btn_reload.click()
        self.assertIn("already up to date", self.dialog.txt_log.toPlainText())

    def test_it_asks_no_host_anything(self):
        # The point of a separate button: a reload must cost no network at all,
        # so it can be pressed as often as the user likes and is not rate
        # limited the way Refresh Now is.
        self.theirs.add_job(make_job("j1", self.host))
        with patch.object(self.service.poller, "tick") as tick:
            self.dialog.btn_reload.click()
        tick.assert_not_called()

    def test_the_selected_row_survives(self):
        self.store.add_job(make_job("keep", self.host))
        self.service.jobs_changed.emit()
        self.dialog.table.selectRow(0)
        self.assertIsNotNone(self.dialog.selected_job())
        self.theirs.reload_jobs()
        self.theirs.add_job(make_job("other", self.host, updated_at=2000.0))
        self.dialog.btn_reload.click()
        selected = self.dialog.selected_job()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "keep")

    def test_a_selection_that_was_removed_elsewhere_is_simply_dropped(self):
        self.store.add_job(make_job("gone", self.host))
        self.service.jobs_changed.emit()
        self.dialog.table.selectRow(0)
        self.theirs.reload_jobs()
        self.theirs.remove_job("gone")
        self.dialog.btn_reload.click()
        self.assertIsNone(self.dialog.selected_job())

    def test_it_is_off_while_an_archive_is_on_screen(self):
        # The table shows a fixed list, so a count of what changed in the live
        # one behind it would describe nothing visible.
        self.dialog._show_archive(os.path.join(self.tmp, "archived.pmejbs"), [])
        self.assertFalse(self.dialog.btn_reload.isEnabled())
        self.dialog._exit_archive()
        self.assertTrue(self.dialog.btn_reload.isEnabled())


class TestTheStandaloneLaunch(unittest.TestCase):
    """`python -m job_manager` must notify exactly as the plugin does."""

    def setUp(self):
        job_manager.shutdown()
        job_manager._context = None
        job_manager._service = None
        self.addCleanup(job_manager.shutdown)
        self.addCleanup(setattr, job_manager, "_service", None)

    def run_main(self, argv, during):
        """Run ``main()`` with ``during(service)`` standing in for the event loop."""
        from job_manager.__main__ import main

        captured = {}

        def fake_exec(app_self=None):
            service = job_manager.get_service(create=False)
            captured["service"] = service
            during(service)
            return 0

        with patch.object(sys, "argv", argv), patch.object(QApplication, "exec", fake_exec):
            captured["rc"] = main()
        return captured

    def test_a_finished_job_raises_a_notification(self):
        """The bug: __main__ built its own JobService, and get_service() is the
        only place that connects job_finished to the notifier -- so a standalone
        monitor polled, downloaded and said nothing at all when a job ended."""
        host = make_host()

        def during(service):
            service.store.add_host(host)
            service.store.add_job(make_job("j1", host, state=STATE_DONE))
            service.job_finished.emit("j1", STATE_DONE)

        with patch("job_manager.notify.notify") as notified:
            self.run_main(["job_manager"], during)
        notified.assert_called_once()
        self.assertIn("j1", notified.call_args[0][1])

    def test_the_chat_room_is_posted_to_as_well(self):
        host = make_host()

        def during(service):
            service.store.add_host(host)
            service.store.set_pref("notify_webhook", "https://hooks.slack.com/services/x")
            service.store.set_pref("notify_chat", True)
            service.store.add_job(make_job("j1", host, state=STATE_DONE))
            service.job_finished.emit("j1", STATE_DONE)

        with patch("job_manager.notify.notify"), patch("job_manager.webhook.post_async") as post:
            self.run_main(["job_manager"], during)
        post.assert_called_once()

    def test_the_service_is_the_module_singleton(self):
        # Which is what makes _notify_finished, reading the module global, find
        # the same store the window is showing.
        captured = self.run_main(["job_manager"], lambda service: None)
        self.assertIsNotNone(captured["service"])

    def test_shutdown_takes_the_tray_icon_down(self):
        # A tray icon outliving the process it belongs to is why the standalone
        # path must go through the module's shutdown(), not service.shutdown().
        with patch("job_manager.notify.shutdown") as tray_shutdown:
            self.run_main(["job_manager"], lambda service: None)
        tray_shutdown.assert_called()

    def test_the_host_monitor_flag_still_opens_the_host_monitor(self):
        from job_manager.host_monitor import HostMonitorDialog

        captured = self.run_main(["job_manager", "--host-monitor"], lambda service: None)
        self.assertEqual(captured["rc"], 0)
        self.assertIsNotNone(captured["service"])
        del HostMonitorDialog


if __name__ == "__main__":
    unittest.main()
