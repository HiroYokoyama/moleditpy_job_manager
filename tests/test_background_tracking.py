"""Tracking jobs without the monitor open, and saying so in the status bar.

The plugin used to build its service only when the monitor window was opened,
so restarting MoleditPy with jobs on a cluster silently stopped tracking every
one of them -- no polling, no auto-download -- until the user happened to open
the window again. It also had no presence in the application at all, so there
was nothing to prompt them to.
"""

import importlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

import job_manager  # noqa: E402
from job_manager.models import (  # noqa: E402
    STATE_FAILED,
    STATE_PENDING,
    STATE_RUNNING,
    Job,
)
from job_manager.service import JobService  # noqa: E402
from job_manager.status_widget import JobStatusWidget, install  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QMainWindow  # noqa: E402


def _click_event(button) -> QMouseEvent:
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(1.0, 1.0),
        QPointF(1.0, 1.0),
        button,
        button,
        Qt.KeyboardModifier.NoModifier,
    )


def make_job(**kwargs) -> Job:
    # auto_download off by default: a job reaching a terminal state would
    # otherwise start a fetch, and a fetch with no host profile emits an error
    # of its own into the very signal these tests are reading.
    defaults = {
        "host_id": "h1",
        "scheduler": "slurm",
        "state": STATE_PENDING,
        "auto_download": False,
    }
    defaults.update(kwargs)
    return Job(**defaults)


class TrackingTestCase(unittest.TestCase):
    """A private data directory, since the plugin reads it at import time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="background_tracking_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._previous_dir = os.environ.get("MOLEDITPY_JOB_MANAGER_DIR")
        os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = self.tmp
        self.addCleanup(self._restore_dir)
        importlib.reload(job_manager)
        self.addCleanup(job_manager.shutdown)
        self.context = MagicMock()
        self.context.get_window.return_value = None

    def _restore_dir(self):
        if self._previous_dir is None:
            os.environ.pop("MOLEDITPY_JOB_MANAGER_DIR", None)
        else:
            os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = self._previous_dir
        job_manager._context = None
        job_manager._service = None
        job_manager._status_widget = None

    def write_jobs(self, *jobs):
        store = JobStore(self.tmp)
        store.jobs = {job.id: job for job in jobs}
        store.save_jobs()
        return store


class TestResumingAfterRestart(TrackingTestCase):
    def test_an_active_job_starts_the_service_at_load(self):
        self.write_jobs(make_job(name="opt", state=STATE_RUNNING, remote_job_id="42"))

        job_manager.initialize(self.context)

        self.assertIsNotNone(job_manager.get_service(create=False))

    def test_the_resumed_service_is_tracking_that_job(self):
        self.write_jobs(make_job(name="opt", state=STATE_RUNNING, remote_job_id="42"))

        job_manager.initialize(self.context)
        service = job_manager.get_service(create=False)

        self.assertEqual([job.name for job in service.store.active_jobs()], ["opt"])

    def test_an_empty_job_list_starts_nothing(self):
        # An empty list must still mean not one byte of network traffic at
        # launch, which is the reason the store is peeked at rather than the
        # service built unconditionally.
        job_manager.initialize(self.context)

        self.assertIsNone(job_manager.get_service(create=False))

    def test_only_finished_jobs_start_nothing(self):
        self.write_jobs(make_job(name="old", state=STATE_FAILED, remote_job_id="1"))

        job_manager.initialize(self.context)

        self.assertIsNone(job_manager.get_service(create=False))

    def test_the_store_is_read_once_not_twice(self):
        # The peek and the service used to build a JobStore each, parsing both
        # files twice at every launch and leaving two views of the same jobs.
        self.write_jobs(make_job(name="opt", state=STATE_RUNNING, remote_job_id="42"))

        with patch("job_manager.store.JobStore.load", autospec=True) as load:
            job_manager.initialize(self.context)

        self.assertEqual(load.call_count, 1)

    def test_an_unreadable_job_list_does_not_break_loading(self):
        with open(os.path.join(self.tmp, "jobs.pmejbs"), "w", encoding="utf-8") as handle:
            handle.write("{ not json")

        job_manager.initialize(self.context)  # must not raise

        self.context.add_plugin_menu.assert_called()


class TestStatusBarIndicator(TrackingTestCase):
    def _service(self, *jobs) -> JobService:
        store = JobStore(self.tmp)
        store.jobs = {job.id: job for job in jobs}
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        return service

    def test_counts_running_queued_and_blocked_separately(self):
        dead = make_job(name="opt", state=STATE_FAILED)
        service = self._service(
            dead,
            make_job(name="run", state=STATE_RUNNING),
            make_job(name="wait", state=STATE_PENDING),
            make_job(name="stuck", state=STATE_PENDING, after_job_id=dead.id),
        )
        widget = JobStatusWidget(service)
        self.addCleanup(widget.detach)

        self.assertEqual(widget.counts(), {"running": 1, "waiting": 1, "blocked": 1})

    def test_the_summary_names_what_is_happening(self):
        service = self._service(make_job(name="run", state=STATE_RUNNING))
        widget = JobStatusWidget(service)
        self.addCleanup(widget.detach)

        self.assertEqual(widget.summary(), "1 running")
        self.assertIn("1 running", widget.text())

    def test_nothing_active_hides_the_widget(self):
        service = self._service(make_job(name="done", state=STATE_FAILED))
        widget = JobStatusWidget(service)
        self.addCleanup(widget.detach)

        self.assertEqual(widget.summary(), "")
        # isHidden(), not isVisible(): a widget whose window was never shown is
        # not "visible" either way, so isVisible() would pass for both cases.
        self.assertTrue(widget.isHidden())

    def test_a_blocked_job_is_called_out_in_the_tooltip(self):
        dead = make_job(name="opt", state=STATE_FAILED)
        service = self._service(dead, make_job(name="stuck", after_job_id=dead.id))
        widget = JobStatusWidget(service)
        self.addCleanup(widget.detach)

        self.assertIn("blocked", widget.summary())
        self.assertIn("never start", widget.toolTip())

    def test_it_updates_when_the_service_says_so(self):
        service = self._service()
        widget = JobStatusWidget(service)
        self.addCleanup(widget.detach)
        self.assertEqual(widget.summary(), "")

        job = make_job(name="new", state=STATE_RUNNING)
        service.store.jobs[job.id] = job
        service.jobs_changed.emit()

        self.assertEqual(widget.summary(), "1 running")

    def test_a_left_click_opens_the_monitor(self):
        service = self._service(make_job(state=STATE_RUNNING))
        clicked = []
        widget = JobStatusWidget(service, on_click=lambda: clicked.append(True))
        self.addCleanup(widget.detach)

        widget.mouseReleaseEvent(_click_event(Qt.MouseButton.LeftButton))

        self.assertEqual(clicked, [True])

    def test_a_right_click_does_not(self):
        service = self._service(make_job(state=STATE_RUNNING))
        clicked = []
        widget = JobStatusWidget(service, on_click=lambda: clicked.append(True))
        self.addCleanup(widget.detach)

        widget.mouseReleaseEvent(_click_event(Qt.MouseButton.RightButton))

        self.assertEqual(clicked, [])

    def test_detaching_stops_it_updating(self):
        # A closed or replaced status bar that still updates is the same leak
        # the monitor window had: the service outlives both.
        service = self._service()
        widget = JobStatusWidget(service)
        widget.detach()

        job = make_job(name="new", state=STATE_RUNNING)
        service.store.jobs[job.id] = job
        service.jobs_changed.emit()

        self.assertEqual(widget.text(), "")

    def test_it_is_added_to_a_real_status_bar(self):
        service = self._service(make_job(state=STATE_RUNNING))
        window = QMainWindow()
        self.addCleanup(window.deleteLater)

        widget = install(window, service)

        self.assertIsNotNone(widget)
        self.assertIn(widget, window.statusBar().findChildren(JobStatusWidget))

    def test_a_host_without_a_status_bar_is_not_an_error(self):
        service = self._service()

        self.assertIsNone(install(object(), service))


class TestTaskBarBadge(TrackingTestCase):
    """The number on the icon in the OS task bar / Dock.

    One Qt call covers macOS's Dock, the Windows task bar button and a Linux
    launcher entry, so the tests assert the call rather than the pixels: what
    the badge looks like is the platform's business, whether it is asked for at
    all is ours.
    """

    def _service(self, *jobs) -> JobService:
        store = JobStore(self.tmp)
        store.jobs = {job.id: job for job in jobs}
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        return service

    def test_the_badge_counts_every_active_job(self):
        service = self._service(
            make_job(name="a", state=STATE_RUNNING),
            make_job(name="b", state=STATE_PENDING),
        )
        with patch("job_manager.taskbar.set_badge") as set_badge:
            widget = JobStatusWidget(service)
            self.addCleanup(widget.detach)

        set_badge.assert_called_with(2)

    def test_nothing_active_asks_for_no_badge(self):
        # 0 is how the platform is told to take the badge off, so it has to be
        # sent rather than skipped.
        service = self._service(make_job(name="old", state=STATE_FAILED))
        with patch("job_manager.taskbar.set_badge") as set_badge:
            widget = JobStatusWidget(service)
            self.addCleanup(widget.detach)

        set_badge.assert_called_with(0)

    def test_the_badge_follows_the_jobs(self):
        service = self._service()
        widget = JobStatusWidget(service)
        self.addCleanup(widget.detach)
        job = make_job(name="new", state=STATE_RUNNING)
        service.store.jobs[job.id] = job

        with patch("job_manager.taskbar.set_badge") as set_badge:
            service.jobs_changed.emit()

        set_badge.assert_called_with(1)

    def test_detaching_clears_the_badge(self):
        service = self._service(make_job(state=STATE_RUNNING))
        widget = JobStatusWidget(service)

        with patch("job_manager.taskbar.set_badge") as set_badge:
            widget.detach()

        set_badge.assert_called_with(0)

    def test_an_old_qt_without_the_api_is_not_an_error(self):
        from job_manager import taskbar

        with patch.object(taskbar, "SUPPORTED", False):
            self.assertFalse(taskbar.set_badge(3))
            self.assertFalse(taskbar.clear_badge())

    def test_a_platform_that_refuses_the_badge_is_not_an_error(self):
        from job_manager import taskbar

        with patch.object(
            taskbar.QGuiApplication, "setBadgeNumber", side_effect=RuntimeError("no badge")
        ):
            self.assertFalse(taskbar.set_badge(3))

    def test_a_negative_count_is_never_sent(self):
        from job_manager import taskbar

        with patch.object(taskbar.QGuiApplication, "setBadgeNumber") as native:
            taskbar.set_badge(-5)

        native.assert_called_once_with(0)

    def test_the_support_flag_matches_the_installed_qt(self):
        # Guards the guard: SUPPORTED going stale in either direction would
        # silently disable every badge while the mocked tests above still pass.
        from job_manager import taskbar

        self.assertEqual(taskbar.SUPPORTED, hasattr(taskbar.QGuiApplication, "setBadgeNumber"))

    def test_the_count_reaches_qt_unchanged(self):
        from job_manager import taskbar

        if not taskbar.SUPPORTED:
            self.skipTest("Qt older than 6.5 has no badge API")
        with patch.object(taskbar.QGuiApplication, "setBadgeNumber") as native:
            self.assertTrue(taskbar.set_badge(4))

        native.assert_called_once_with(4)


class TestStrandedChainIsAnnounced(TrackingTestCase):
    def test_a_failure_says_which_jobs_will_never_start(self):
        dead = make_job(name="opt", state=STATE_RUNNING)
        stranded = make_job(name="freq", after_job_id=dead.id)
        store = JobStore(self.tmp)
        store.jobs = {dead.id: dead, stranded.id: stranded}
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        messages = []
        service.error.connect(messages.append)

        dead.touch(STATE_FAILED)
        service._on_job_state_changed(dead.id, STATE_FAILED)

        self.assertTrue(any("freq" in text and "never start" in text for text in messages))

    def test_nothing_is_said_when_the_chain_releases_anyway(self):
        dead = make_job(name="opt", scheduler="shell", state=STATE_RUNNING)
        follower = make_job(name="next", scheduler="shell", after_job_id=dead.id)
        store = JobStore(self.tmp)
        store.jobs = {dead.id: dead, follower.id: follower}
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        messages = []
        service.error.connect(messages.append)

        dead.touch(STATE_FAILED)
        service._on_job_state_changed(dead.id, STATE_FAILED)

        self.assertEqual(messages, [])

    def test_a_successful_job_says_nothing(self):
        first = make_job(name="opt", state=STATE_RUNNING)
        second = make_job(name="freq", after_job_id=first.id)
        store = JobStore(self.tmp)
        store.jobs = {first.id: first, second.id: second}
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        messages = []
        service.error.connect(messages.append)

        first.touch("DONE")
        service._on_job_state_changed(first.id, "DONE")

        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
