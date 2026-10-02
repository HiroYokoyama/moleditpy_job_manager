"""The tray, the task bar and the title: what they are told to show, and when."""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

import job_manager  # noqa: E402
from job_manager import presence  # noqa: E402
from job_manager.models import (  # noqa: E402
    STATE_DONE,
    STATE_FAILED,
    STATE_LOST,
    STATE_PENDING,
    STATE_RUNNING,
    Job,
)
from job_manager.presence import (  # noqa: E402
    Presence,
    Progress,
    count_jobs,
    summary_text,
    taskbar_progress,
    title_prefix,
    tray_state,
)
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402


def make_job(**kwargs) -> Job:
    defaults = {
        "host_id": "h1",
        "scheduler": "slurm",
        "state": STATE_PENDING,
        "auto_download": False,
    }
    defaults.update(kwargs)
    return Job(**defaults)


def counts(running=0, waiting=0, blocked=0) -> dict:
    return {"running": running, "waiting": waiting, "blocked": blocked}


class TestWords(unittest.TestCase):
    def test_summary_names_only_what_is_there(self):
        self.assertEqual(summary_text(counts(running=2, blocked=1)), "2 running  1 blocked")
        self.assertEqual(summary_text(counts()), "")

    def test_summary_takes_a_separator(self):
        self.assertEqual(summary_text(counts(1, 2), ", "), "1 running, 2 queued")

    def test_the_title_prefix_is_empty_when_idle(self):
        # An idle monitor keeps its plain title rather than "0 running - ...".
        self.assertEqual(title_prefix(counts()), "")
        self.assertEqual(title_prefix(counts(running=3)), "3 running - ")


class TestProgress(unittest.TestCase):
    def test_idle_shows_nothing(self):
        self.assertEqual(taskbar_progress(counts(), 0, 0), Progress())

    def test_idle_after_a_failure_stays_red_until_seen(self):
        self.assertEqual(taskbar_progress(counts(), 0, 1), Progress("error", 1, 1))

    def test_one_job_pulses(self):
        # There is no fraction of one job to draw.
        self.assertEqual(taskbar_progress(counts(running=1), 1, 0).state, "indeterminate")

    def test_a_batch_fills_as_it_finishes(self):
        self.assertEqual(taskbar_progress(counts(running=2), 5, 0), Progress("normal", 3, 5))

    def test_a_batch_where_nothing_ended_yet_pulses(self):
        self.assertEqual(
            taskbar_progress(counts(running=2, waiting=3), 5, 0).state, "indeterminate"
        )

    def test_a_blocked_chain_is_yellow(self):
        self.assertEqual(taskbar_progress(counts(running=1, blocked=1), 4, 0).state, "paused")

    def test_a_failure_outranks_a_blocked_chain(self):
        self.assertEqual(taskbar_progress(counts(running=1, blocked=1), 4, 1).state, "error")

    def test_a_coloured_bar_is_never_empty(self):
        # Red or yellow with 0 filled in draws nothing at all on the button.
        progress = taskbar_progress(counts(blocked=2), 2, 0)
        self.assertEqual((progress.state, progress.value, progress.total), ("paused", 2, 2))

    def test_a_batch_total_smaller_than_the_active_count_is_not_negative(self):
        progress = taskbar_progress(counts(running=3), 1, 0)
        self.assertEqual(progress.state, "indeterminate")


class TestTrayState(unittest.TestCase):
    def test_states(self):
        self.assertEqual(tray_state(counts(), 0), "idle")
        self.assertEqual(tray_state(counts(waiting=1), 0), "queued")
        self.assertEqual(tray_state(counts(running=1, waiting=1), 0), "busy")
        self.assertEqual(tray_state(counts(running=1, blocked=1), 0), "error")
        self.assertEqual(tray_state(counts(), 2), "error")


class PresenceTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="presence_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def service(self, *jobs, **prefs) -> JobService:
        store = JobStore(self.tmp)
        store.jobs = {job.id: job for job in jobs}
        store.prefs.update(prefs)
        service = JobService(store=store)
        self.addCleanup(service.shutdown)
        return service

    def presence(self, service, main_window=None) -> Presence:
        # No tray on an offscreen desktop; the tray has its own tests.
        with patch("job_manager.notify.available", return_value=False):
            result = Presence(service, main_window)
        self.addCleanup(result.detach)
        return result


class TestCounting(PresenceTestCase):
    def test_count_jobs_splits_running_from_waiting(self):
        service = self.service(
            make_job(name="a", state=STATE_RUNNING),
            make_job(name="b", state=STATE_PENDING),
            make_job(name="c", state=STATE_DONE),
        )
        self.assertEqual(count_jobs(service.store), counts(running=1, waiting=1))


class TestUnseenFailures(PresenceTestCase):
    def test_a_failure_is_counted_until_acknowledged(self):
        service = self.service()
        shown = self.presence(service)
        service.job_finished.emit("x", STATE_FAILED)
        service.job_finished.emit("y", STATE_LOST)
        self.assertEqual(shown.unseen_failures, 2)

        shown.acknowledge()

        self.assertEqual(shown.unseen_failures, 0)

    def test_a_job_that_finished_well_is_not_a_failure(self):
        service = self.service()
        shown = self.presence(service)
        service.job_finished.emit("x", STATE_DONE)
        self.assertEqual(shown.unseen_failures, 0)

    def test_detached_it_stops_listening(self):
        service = self.service()
        shown = self.presence(service)
        shown.detach()
        service.job_finished.emit("x", STATE_FAILED)
        self.assertEqual(shown.unseen_failures, 0)


class TestWindows(PresenceTestCase):
    def test_a_registered_window_is_given_the_progress(self):
        service = self.service(make_job(state=STATE_RUNNING))
        shown = self.presence(service)
        window = MagicMock()

        shown.add_window(window)

        window.set_progress.assert_called_with("indeterminate", 0, 0)

    def test_progress_switched_off_clears_it(self):
        service = self.service(make_job(state=STATE_RUNNING), taskbar_progress=False)
        shown = self.presence(service)
        window = MagicMock()

        shown.add_window(window)

        window.set_progress.assert_not_called()
        window.clear.assert_called()

    def test_a_removed_window_is_cleared_and_forgotten(self):
        service = self.service(make_job(state=STATE_RUNNING))
        shown = self.presence(service)
        window = MagicMock()
        shown.add_window(window)

        shown.remove_window(window)
        window.reset_mock()
        service.jobs_changed.emit()

        window.set_progress.assert_not_called()

    def test_the_batch_resets_when_the_list_goes_idle(self):
        job_a = make_job(name="a", state=STATE_RUNNING)
        job_b = make_job(name="b", state=STATE_RUNNING)
        service = self.service(job_a, job_b)
        shown = self.presence(service)
        window = MagicMock()
        shown.add_window(window)

        job_a.state = STATE_DONE
        service.jobs_changed.emit()
        window.set_progress.assert_called_with("normal", 1, 2)

        job_b.state = STATE_DONE
        service.jobs_changed.emit()
        window.set_progress.assert_called_with("none", 0, 0)

        # A new job after idle is a new batch of one, not 1 of 3.
        job_c = make_job(name="c", state=STATE_RUNNING)
        service.store.jobs[job_c.id] = job_c
        service.jobs_changed.emit()
        window.set_progress.assert_called_with("indeterminate", 0, 0)

    def test_title_listeners_get_the_counts(self):
        service = self.service(make_job(state=STATE_RUNNING))
        shown = self.presence(service)
        seen = []
        shown.add_title_listener(seen.append)

        service.jobs_changed.emit()

        self.assertEqual(seen[-1], counts(running=1))
        shown.remove_title_listener(seen.append)

    def test_a_failing_listener_does_not_stop_the_rest(self):
        service = self.service()
        shown = self.presence(service)
        seen = []
        shown.add_title_listener(MagicMock(side_effect=RuntimeError("deleted")))
        shown.add_title_listener(seen.append)

        service.jobs_changed.emit()

        self.assertTrue(seen)


class TestMainWindowProgress(PresenceTestCase):
    def test_moleditpys_own_button_is_left_alone_by_default(self):
        service = self.service(make_job(state=STATE_RUNNING))
        with (
            patch("job_manager.win_taskbar.AVAILABLE", True),
            patch("job_manager.win_taskbar.set_progress") as native,
        ):
            self.presence(service, main_window=MagicMock())
        native.assert_not_called()

    def test_it_follows_the_badge_opt_in(self):
        service = self.service(make_job(state=STATE_RUNNING), taskbar_badge=True)
        main = MagicMock()
        main.winId.return_value = 99
        with (
            patch("job_manager.win_taskbar.AVAILABLE", True),
            patch("job_manager.win_taskbar.set_progress") as native,
        ):
            shown = self.presence(service, main_window=main)
            native.assert_called_with(99, "indeterminate", 0, 0)

            service.store.set_pref("taskbar_badge", False)
            service.jobs_changed.emit()
            native.assert_called_with(99, "none", 0, 0)
            shown.detach()


class TestAlert(PresenceTestCase):
    def test_a_job_ending_flashes_the_main_window(self):
        service = self.service()
        main = QWidget()
        self.addCleanup(main.deleteLater)
        main.show()
        shown = self.presence(service, main_window=main)
        with patch("PyQt6.QtWidgets.QApplication.alert") as alert:
            service.job_finished.emit("x", STATE_DONE)
        alert.assert_called_once_with(main, 0)
        del shown

    def test_the_open_monitor_is_flashed_instead(self):
        service = self.service()
        main = QWidget()
        monitor = QWidget()
        for widget in (main, monitor):
            self.addCleanup(widget.deleteLater)
            widget.show()
        shown = self.presence(service, main_window=main)
        window = MagicMock()
        window.widget = monitor
        shown.add_window(window)
        with patch("PyQt6.QtWidgets.QApplication.alert") as alert:
            service.job_finished.emit("x", STATE_DONE)
        alert.assert_called_once_with(monitor, 0)

    def test_it_can_be_switched_off(self):
        service = self.service(flash_on_finish=False)
        main = QWidget()
        self.addCleanup(main.deleteLater)
        main.show()
        self.presence(service, main_window=main)
        with patch("PyQt6.QtWidgets.QApplication.alert") as alert:
            service.job_finished.emit("x", STATE_DONE)
        alert.assert_not_called()

    def test_nothing_visible_means_nothing_flashed(self):
        service = self.service()
        main = QWidget()
        self.addCleanup(main.deleteLater)
        self.presence(service, main_window=main)
        with patch("PyQt6.QtWidgets.QApplication.alert") as alert:
            service.job_finished.emit("x", STATE_DONE)
        alert.assert_not_called()


class TestTheMonitorWindow(PresenceTestCase):
    """The job monitor's half: counts in its title, its own task bar button."""

    def dialog(self, service):
        from job_manager.jobs_dialog import JobsDialog

        dialog = JobsDialog(service)
        self.addCleanup(dialog.close)
        return dialog

    def installed(self, service) -> Presence:
        with patch("job_manager.notify.available", return_value=False):
            shown = presence.install(service)
        self.addCleanup(presence.uninstall)
        return shown

    def test_the_title_leads_with_the_counts(self):
        service = self.service(make_job(state=STATE_RUNNING))
        self.installed(service)
        dialog = self.dialog(service)
        self.assertTrue(dialog.windowTitle().startswith("1 running - Job Manager "))

    def test_the_title_follows_the_jobs(self):
        job = make_job(state=STATE_RUNNING)
        service = self.service(job)
        self.installed(service)
        dialog = self.dialog(service)

        job.state = STATE_DONE
        service.jobs_changed.emit()

        self.assertTrue(dialog.windowTitle().startswith("Job Manager "))

    def test_without_a_presence_the_title_still_counts(self):
        service = self.service(make_job(state=STATE_RUNNING))
        dialog = self.dialog(service)
        self.assertTrue(dialog.windowTitle().startswith("1 running - "))

    def test_a_list_file_keeps_the_counts_in_front(self):
        service = self.service(make_job(state=STATE_RUNNING))
        dialog = self.dialog(service)
        dialog._set_base_title("Job Manager x - other.pmejbs")
        self.assertEqual(dialog.windowTitle(), "1 running - Job Manager x - other.pmejbs")

    def test_its_task_bar_button_is_registered_and_released(self):
        service = self.service(make_job(state=STATE_RUNNING))
        shown = self.installed(service)
        dialog = self.dialog(service)
        self.assertIn(dialog._taskbar, shown._windows)

        dialog.close()

        self.assertNotIn(dialog._taskbar, shown._windows)
        # And no longer told about job changes.
        service.jobs_changed.emit()

    def test_it_offers_three_thumbnail_buttons(self):
        service = self.service()
        dialog = self.dialog(service)
        ids = [button[0] for button in dialog._taskbar.buttons]
        self.assertEqual(ids, [1, 2, 3])
        with patch.object(service.poller, "refresh_now", return_value=True) as refresh:
            dialog._taskbar.buttons[0][3]()
        refresh.assert_called_once()

    def test_activating_it_acknowledges_failures(self):
        from PyQt6.QtCore import QEvent

        service = self.service()
        shown = self.installed(service)
        dialog = self.dialog(service)
        shown.unseen_failures = 2
        with patch.object(dialog, "isActiveWindow", return_value=True):
            dialog.changeEvent(QEvent(QEvent.Type.ActivationChange))
        self.assertEqual(shown.unseen_failures, 0)

    def test_select_job_clears_a_filter_that_hides_it(self):
        wanted = make_job(name="wanted", state=STATE_RUNNING)
        service = self.service(wanted, make_job(name="other", state=STATE_RUNNING))
        dialog = self.dialog(service)
        dialog.txt_filter.setText("other")

        dialog.select_job(wanted.id)

        self.assertEqual(dialog.txt_filter.text(), "")
        self.assertEqual(dialog.selected_job().id, wanted.id)

    def test_select_job_ignores_an_unknown_id(self):
        service = self.service(make_job(state=STATE_RUNNING))
        dialog = self.dialog(service)
        dialog.select_job("nope")
        self.assertIsNone(dialog.selected_job())

    def test_a_native_event_never_hands_back_the_base_result(self):
        # Returning super().nativeEvent()'s tuple crashed the real Windows
        # platform before the window had finished opening.
        from PyQt6.QtWidgets import QDialog

        service = self.service()
        dialog = self.dialog(service)
        with patch.object(QDialog, "nativeEvent", side_effect=AssertionError("called")):
            self.assertEqual(dialog.nativeEvent(b"windows_generic_MSG", 0), (False, 0))
            self.assertEqual(dialog.nativeEvent(b"xcb_generic_event_t", 0), (False, 0))

    def test_a_native_event_off_windows_is_left_to_qt(self):
        service = self.service()
        dialog = self.dialog(service)
        handled, _result = dialog.nativeEvent(b"windows_generic_MSG", 0)
        self.assertFalse(handled)


class TestPluginWiring(unittest.TestCase):
    """The plugin builds one presence with its service and takes it down."""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="presence_plugin_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        previous = os.environ.get("MOLEDITPY_JOB_MANAGER_DIR")
        os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = self.tmp

        def restore():
            if previous is None:
                os.environ.pop("MOLEDITPY_JOB_MANAGER_DIR", None)
            else:
                os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = previous
            job_manager._context = None

        self.addCleanup(restore)
        importlib.reload(job_manager)
        self.addCleanup(job_manager.shutdown)
        self.context = MagicMock()
        self.context.get_window.return_value = None
        job_manager._context = self.context

    def test_the_service_brings_a_presence_and_shutdown_removes_it(self):
        job_manager.get_service()
        self.assertIsNotNone(presence.current())

        job_manager.shutdown()

        self.assertIsNone(presence.current())

    def test_showing_the_monitor_acknowledges_failures(self):
        job_manager.get_service()
        current = presence.current()
        current.unseen_failures = 3
        window = MagicMock()
        window.isMinimized.return_value = True
        self.context.get_window.return_value = window

        job_manager.show_monitor(self.context)

        window.showNormal.assert_called_once()
        self.assertEqual(current.unseen_failures, 0)

    def test_show_job_selects_it_in_the_monitor(self):
        window = MagicMock()
        window.isMinimized.return_value = False
        self.context.get_window.return_value = window

        job_manager.show_job("abc", self.context)

        window.select_job.assert_called_once_with("abc")

    def test_the_new_preferences_have_safe_defaults(self):
        store = JobStore(self.tmp)
        # Closing MoleditPy has always quit it; staying behind is opt-in.
        self.assertFalse(store.get_pref("keep_running_in_tray"))
        self.assertTrue(store.get_pref("flash_on_finish"))
        self.assertTrue(store.get_pref("taskbar_progress"))


if __name__ == "__main__":
    unittest.main()
