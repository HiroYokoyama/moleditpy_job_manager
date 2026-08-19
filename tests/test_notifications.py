"""Telling the user a job ended, when they are not watching the window.

The badge and the status bar counter both answer "how many are running", which
is a number you have to go and look at. Everything here is about the moment of
transition instead -- raised once, only for a real change, and only when the
user has left it on.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

import job_manager  # noqa: E402
from job_manager import notify  # noqa: E402
from job_manager.models import (  # noqa: E402
    STATE_DONE,
    STATE_FAILED,
    STATE_LOST,
    STATE_RUNNING,
)
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

from .fakes import FakeTransport, make_host, make_job  # noqa: E402
from .test_poller import SyncPool  # noqa: E402


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="notify_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(self.tmp)
        self.service = JobService(self.store)
        self.service.pool = SyncPool()
        self.service.poller.pool = SyncPool()
        self.addCleanup(self.service.shutdown)


class TestTheServiceAnnouncesTheEnd(ServiceTestCase):
    def announced(self, state: str, job=None):
        # auto_download on, because that is the default and what every real
        # submitted job has. With it off, this suite passed while the common
        # path announced nothing at all: download() moves the job to
        # DOWNLOADING before the terminal check could see the state.
        job = job or make_job(id="j1", auto_download=True)
        self.store.add_job(job)
        self.store.hosts.setdefault(job.host_id, make_host(id=job.host_id))
        seen = []
        self.service.job_finished.connect(lambda job_id, s: seen.append((job_id, s)))
        job.touch(state)
        self.service._on_job_state_changed(job.id, state)
        return seen

    def test_a_finished_job_is_announced(self):
        self.assertEqual(self.announced(STATE_DONE), [("j1", STATE_DONE)])

    def test_a_failed_job_is_announced_too(self):
        # A six-hour job that failed is the case where being told matters most.
        self.assertEqual(self.announced(STATE_FAILED), [("j1", STATE_FAILED)])

    def test_a_lost_job_is_announced(self):
        self.assertEqual(self.announced(STATE_LOST), [("j1", STATE_LOST)])

    def test_a_job_that_merely_started_running_is_not(self):
        self.assertEqual(self.announced(STATE_RUNNING), [])

    def test_a_job_that_left_the_store_is_not_announced(self):
        seen = []
        self.service.job_finished.connect(lambda job_id, s: seen.append(job_id))
        self.service._on_job_state_changed("gone", STATE_DONE)
        self.assertEqual(seen, [])


class DeferredPool:
    """A pool that queues tasks instead of running them.

    ``SyncPool`` runs every task inline, so an auto-download starts *and
    finishes* inside the call that triggered it and the job is back at DONE by
    the time anything looks. A real QThreadPool does not: the job sits at
    DOWNLOADING. That difference hid a bug where nothing was announced at all,
    so these tests need a pool that behaves like the real one.
    """

    def __init__(self):
        self.pending = []

    def setMaxThreadCount(self, count):
        pass

    def start(self, task):
        self.pending.append(task)

    def drain(self):
        while self.pending:
            self.pending.pop(0).run_sync()

    def clear(self):
        self.pending = []

    def waitForDone(self, msecs=0):
        return True


class TestAnAutoDownloadDoesNotSwallowTheAnnouncement(ServiceTestCase):
    """The default configuration, with a pool that really defers."""

    def setUp(self):
        super().setUp()
        self.service.pool = DeferredPool()
        self.host = make_host()
        self.store.hosts[self.host.id] = self.host
        self.service.transport_for = lambda host: FakeTransport(self.host)

    def announce(self, state, **kwargs):
        job = make_job(id="j1", auto_download=True, **kwargs)
        self.store.add_job(job)
        seen = []
        self.service.job_finished.connect(lambda job_id, s: seen.append((job_id, s)))
        job.touch(state)
        self.service._on_job_state_changed(job.id, state)
        return job, seen

    def test_the_download_really_is_still_in_flight(self):
        # Guards the guard: if this stops being true the tests below go back to
        # passing for the wrong reason.
        job, _seen = self.announce(STATE_DONE)
        self.assertEqual(job.state, "DOWNLOADING")

    def test_a_finished_job_is_still_announced(self):
        _job, seen = self.announce(STATE_DONE)
        self.assertEqual(seen, [("j1", STATE_DONE)])

    def test_a_failed_job_is_still_announced(self):
        _job, seen = self.announce(STATE_FAILED)
        self.assertEqual(seen, [("j1", STATE_FAILED)])

    def test_the_stranding_warning_still_reaches_the_user(self):
        # The case it exists for: a FAILED job with jobs chained behind it.
        # Auto-download moved the job to DOWNLOADING first, so the check for a
        # terminal state saw one that was not, and nobody was told.
        blocked = make_job(id="j2", name="second", after_job_id="j1", state="PENDING")
        self.store.add_job(blocked)
        errors = []
        self.service.error.connect(errors.append)

        self.announce(STATE_FAILED)

        self.assertTrue(any("second" in message for message in errors), errors)


class TestTheHandlerRespectsThePreference(ServiceTestCase):
    """The setting is read at the moment of the event, not cached."""

    def setUp(self):
        super().setUp()
        self.store.add_job(make_job(id="j1", name="opt", host_name="cluster"))
        # The plugin module owns the handler, so it is wired to this service
        # for the duration of the test and put back afterwards.
        self._previous = job_manager._service
        job_manager._service = self.service
        self.addCleanup(setattr, job_manager, "_service", self._previous)

    def test_it_notifies_by_default(self):
        with patch.object(notify, "notify", return_value=True) as raised:
            job_manager._notify_finished("j1", STATE_DONE)
        raised.assert_called_once()

    def test_the_message_names_the_job_and_the_host(self):
        with patch.object(notify, "notify", return_value=True) as raised:
            job_manager._notify_finished("j1", STATE_DONE)
        _title, message = raised.call_args[0]
        self.assertIn("opt", message)
        self.assertIn("cluster", message)

    def test_a_failure_does_not_read_as_a_success(self):
        with patch.object(notify, "notify", return_value=True) as raised:
            job_manager._notify_finished("j1", STATE_FAILED)
        self.assertIn("failed", raised.call_args[0][1])

    def test_turning_it_off_stops_it(self):
        self.store.set_pref("notify_on_finish", False)
        with patch.object(notify, "notify", return_value=True) as raised:
            job_manager._notify_finished("j1", STATE_DONE)
        raised.assert_not_called()

    def test_a_job_that_no_longer_exists_raises_nothing(self):
        with patch.object(notify, "notify", return_value=True) as raised:
            job_manager._notify_finished("vanished", STATE_DONE)
        raised.assert_not_called()

    def test_a_desktop_that_refuses_is_not_an_error(self):
        # A headless session, a window manager with no tray: normal outcomes,
        # not something to interrupt the user about.
        with patch.object(notify, "notify", side_effect=RuntimeError("no tray")):
            job_manager._notify_finished("j1", STATE_DONE)


class TestTheChatRoomGetsItToo(ServiceTestCase):
    """The webhook is the notification for when nobody is at this desktop."""

    def setUp(self):
        super().setUp()
        from job_manager import webhook

        self.webhook = webhook
        self.store.add_job(make_job(id="j1", name="opt", host_name="cluster"))
        self._previous = job_manager._service
        job_manager._service = self.service
        self.addCleanup(setattr, job_manager, "_service", self._previous)

    def test_nothing_is_posted_until_a_url_is_configured(self):
        with patch.object(self.webhook, "post_async") as posted:
            with patch.object(notify, "notify", return_value=True):
                job_manager._notify_finished("j1", STATE_DONE)
        # Called, and refused there: one place decides what is a URL.
        self.assertEqual(posted.call_args[0][0], "")

    def test_a_configured_url_gets_the_same_sentence(self):
        self.store.set_pref("notify_webhook", "https://hooks.slack.com/services/T/B/x")
        with patch.object(self.webhook, "post_async") as posted:
            with patch.object(notify, "notify", return_value=True):
                job_manager._notify_finished("j1", STATE_DONE)
        url, _title, message = posted.call_args[0]
        self.assertEqual(url, "https://hooks.slack.com/services/T/B/x")
        self.assertIn("opt", message)
        self.assertIn("cluster", message)

    def test_a_desktop_with_no_tray_still_posts_to_the_room(self):
        # The whole point: the tray refusing is exactly the session where the
        # chat message is the only one that will be seen.
        self.store.set_pref("notify_webhook", "https://hooks.slack.com/services/T/B/x")
        with patch.object(self.webhook, "post_async") as posted:
            with patch.object(notify, "notify", side_effect=RuntimeError("no tray")):
                job_manager._notify_finished("j1", STATE_DONE)
        posted.assert_called_once()

    def test_turning_notifications_off_stops_the_chat_message_as_well(self):
        self.store.set_pref("notify_webhook", "https://hooks.slack.com/services/T/B/x")
        self.store.set_pref("notify_on_finish", False)
        with patch.object(self.webhook, "post_async") as posted:
            job_manager._notify_finished("j1", STATE_DONE)
        posted.assert_not_called()

    def test_a_webhook_that_explodes_is_not_an_error(self):
        self.store.set_pref("notify_webhook", "https://hooks.slack.com/services/T/B/x")
        with patch.object(self.webhook, "post_async", side_effect=RuntimeError("boom")):
            with patch.object(notify, "notify", return_value=True):
                job_manager._notify_finished("j1", STATE_DONE)


class TestTheNotifierItself(unittest.TestCase):
    def tearDown(self):
        notify.shutdown()

    def test_nothing_is_raised_where_there_is_no_tray(self):
        with patch.object(notify, "available", return_value=False):
            self.assertFalse(notify.notify("t", "m"))

    def test_no_tray_icon_is_created_at_import(self):
        # Creating one on load would put an icon in the user's tray for a
        # plugin they may never use.
        self.assertIsNone(notify._tray)

    def test_shutdown_is_safe_when_nothing_was_ever_shown(self):
        notify.shutdown()
        self.assertIsNone(notify._tray)

    def test_a_refused_call_reports_false_rather_than_raising(self):
        with patch.object(notify, "available", return_value=True):
            with patch("job_manager.notify.QSystemTrayIcon", side_effect=RuntimeError("no")):
                self.assertFalse(notify.notify("t", "m"))

    def test_a_platform_that_cannot_be_asked_is_simply_unavailable(self):
        with patch("job_manager.notify.QSystemTrayIcon") as tray_class:
            tray_class.isSystemTrayAvailable.side_effect = RuntimeError("no display")
            self.assertFalse(notify.available())


class TestTheTrayIcon(unittest.TestCase):
    """The parts that need a tray, driven against a stand-in for one."""

    def setUp(self):
        self.tray = MagicMock()
        patcher = patch("job_manager.notify.QSystemTrayIcon", return_value=self.tray)
        self.tray_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.tray_class.isSystemTrayAvailable.return_value = True
        self.addCleanup(notify.shutdown)

    def test_the_message_reaches_the_tray(self):
        self.assertTrue(notify.notify("MoleditPy", "opt finished"))
        self.tray.showMessage.assert_called_once()
        self.assertEqual(self.tray.showMessage.call_args[0][1], "opt finished")

    def test_the_icon_is_shown_before_a_message_is_put_on_it(self):
        notify.notify("t", "m")
        self.tray.show.assert_called_once()

    def test_a_second_notification_reuses_the_one_icon(self):
        # One icon per session, not one per finished job.
        notify.notify("t", "first")
        notify.notify("t", "second")
        self.assertEqual(self.tray_class.call_count, 1)
        self.assertEqual(self.tray.showMessage.call_count, 2)

    def test_it_is_named_so_the_user_can_tell_whose_it_is(self):
        notify.notify("t", "m")
        self.assertIn("MoleditPy", self.tray.setToolTip.call_args[0][0])

    def test_shutdown_takes_the_icon_away(self):
        notify.notify("t", "m")

        notify.shutdown()

        self.tray.hide.assert_called_once()
        self.assertIsNone(notify._tray)

    def test_a_notification_after_shutdown_makes_a_new_icon(self):
        notify.notify("t", "m")
        notify.shutdown()

        notify.notify("t", "again")

        self.assertEqual(self.tray_class.call_count, 2)

    def test_an_icon_that_refuses_to_go_is_still_forgotten(self):
        # Otherwise the module would hold a reference to a dead C++ object and
        # every later notification would raise.
        notify.notify("t", "m")
        self.tray.hide.side_effect = RuntimeError("already deleted")

        notify.shutdown()

        self.assertIsNone(notify._tray)

    def test_a_tray_that_rejects_the_message_reports_false(self):
        self.tray.showMessage.side_effect = RuntimeError("refused")
        self.assertFalse(notify.notify("t", "m"))


class TestThePreferenceDefault(unittest.TestCase):
    def test_it_is_on_out_of_the_box(self):
        tmp = tempfile.mkdtemp(prefix="pref_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertTrue(JobStore(tmp).get_pref("notify_on_finish"))

    def test_the_badge_is_still_off_out_of_the_box(self):
        # Unchanged on purpose: a notification is transient, the badge is a
        # lasting change to how MoleditPy looks in the task bar.
        tmp = tempfile.mkdtemp(prefix="pref_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertFalse(JobStore(tmp).get_pref("taskbar_badge"))


if __name__ == "__main__":
    unittest.main()
