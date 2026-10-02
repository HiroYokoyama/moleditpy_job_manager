"""The one Settings window: every standing preference, written as it changes."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication, QGroupBox  # noqa: E402

from job_manager.service import JobService  # noqa: E402
from job_manager.settings_dialog import KEEP_TRACKING_TEXT, SettingsDialog  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

WEBHOOK = "https://hooks.slack.com/services/T/B/x"


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="settings_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(self.tmp)
        self.service = JobService(store=self.store)
        self.addCleanup(self.service.shutdown)

    def dialog(self, **kwargs) -> SettingsDialog:
        dialog = SettingsDialog(self.service, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def saved(self, key):
        """Read back from disk: a preference only in memory is lost on restart."""
        return JobStore(self.tmp).get_pref(key)


class TestItHoldsEverything(SettingsTestCase):
    def test_the_groups(self):
        titles = [group.title() for group in self.dialog().findChildren(QGroupBox)]
        self.assertEqual(
            titles,
            ["Polling", "Results", "When a job ends", "Desktop", "Web Monitor", "Local API"],
        )

    def test_the_tray_process_has_no_api_to_offer(self):
        # The API is served by MoleditPy; switching it on from the tray process
        # would start a second server there.
        titles = [g.title() for g in self.dialog(standalone=True).findChildren(QGroupBox)]
        self.assertNotIn("Local API", titles)

    def test_it_reflects_what_is_stored(self):
        self.store.set_pref("notify_on_finish", False)
        self.store.set_pref("keep_running_in_tray", True)
        self.store.set_pref("taskbar_badge", True)
        dialog = self.dialog()
        self.assertFalse(dialog.chk_notify.isChecked())
        self.assertTrue(dialog.chk_keep_tracking.isChecked())
        self.assertTrue(dialog.chk_taskbar_badge.isChecked())
        self.assertTrue(dialog.chk_auto_open.isChecked())
        self.assertTrue(dialog.chk_flash.isChecked())

    def test_keep_tracking_is_named_as_the_docs_name_it(self):
        self.assertEqual(self.dialog().chk_keep_tracking.text(), KEEP_TRACKING_TEXT)


class TestPolling(SettingsTestCase):
    def test_the_spinbox_reflects_the_store(self):
        self.assertEqual(self.dialog().spin_interval.value(), self.store.poll_interval)

    def test_the_floor_is_enforced_by_the_widget(self):
        dialog = self.dialog()
        dialog.spin_interval.setValue(0)
        self.assertGreaterEqual(dialog.spin_interval.value(), 5)

    def test_a_fast_interval_is_accepted_but_flagged(self):
        dialog = self.dialog()
        dialog.spin_interval.setValue(10)
        self.assertEqual(self.store.poll_interval, 10)
        self.assertTrue(dialog.lbl_interval_warning.text())
        self.assertIn("login node", dialog.lbl_interval_warning.toolTip())

    def test_the_warning_clears_when_the_interval_is_courteous(self):
        dialog = self.dialog()
        dialog.spin_interval.setValue(10)
        dialog.spin_interval.setValue(120)
        self.assertEqual(dialog.lbl_interval_warning.text(), "")

    def test_no_warning_at_the_default(self):
        self.assertEqual(self.dialog().lbl_interval_warning.text(), "")

    def test_a_change_is_saved_and_the_poller_rescheduled(self):
        dialog = self.dialog()
        with patch.object(self.service.poller, "reschedule") as reschedule:
            dialog.spin_interval.setValue(300)
        reschedule.assert_called_once()
        self.assertEqual(self.saved("poll_interval"), 300)


class TestResults(SettingsTestCase):
    def test_auto_open_is_saved(self):
        self.dialog().chk_auto_open.setChecked(False)
        self.assertFalse(self.saved("open_result_after_download"))

    def test_beside_the_input_is_saved(self):
        self.dialog().chk_beside_input.setChecked(False)
        self.assertFalse(self.saved("download_beside_input"))

    def test_the_download_folder_is_saved_when_editing_ends(self):
        dialog = self.dialog()
        dialog.txt_download_root.setText("  /opt/results  ")
        self.assertEqual(self.saved("download_root"), "")
        dialog.txt_download_root.editingFinished.emit()
        self.assertEqual(self.saved("download_root"), "/opt/results")

    def test_browsing_saves_the_choice(self):
        dialog = self.dialog()
        with patch(
            "job_manager.settings_dialog.QFileDialog.getExistingDirectory", return_value="/opt/res"
        ):
            dialog._browse_download_root()
        self.assertEqual(self.saved("download_root"), "/opt/res")

    def test_a_cancelled_browse_changes_nothing(self):
        self.store.set_pref("download_root", "/opt/keep")
        dialog = self.dialog()
        with patch("job_manager.settings_dialog.QFileDialog.getExistingDirectory", return_value=""):
            dialog._browse_download_root()
        self.assertEqual(self.saved("download_root"), "/opt/keep")


class TestWhenAJobEnds(SettingsTestCase):
    def test_notify_and_flash_are_saved(self):
        dialog = self.dialog()
        dialog.chk_notify.setChecked(False)
        dialog.chk_flash.setChecked(False)
        self.assertFalse(self.saved("notify_on_finish"))
        self.assertFalse(self.saved("flash_on_finish"))

    def test_the_chat_tick_is_unusable_until_a_room_is_set(self):
        # A tick that can be set with nothing behind it claims messages are
        # going out while none are, and only a job ending disproves it.
        dialog = self.dialog()
        self.assertFalse(dialog.chk_chat.isEnabled())
        self.assertFalse(dialog.chk_chat.isChecked())

    def test_the_chat_tick_is_saved(self):
        self.store.set_pref("notify_webhook", WEBHOOK)
        dialog = self.dialog()
        self.assertTrue(dialog.chk_chat.isEnabled())
        dialog.chk_chat.setChecked(True)
        self.assertTrue(self.saved("notify_chat"))

    def test_syncing_does_not_write_the_setting_back(self):
        # It runs whenever the URL might have changed; letting setChecked
        # through would overwrite the user's own choice on every open.
        self.store.set_pref("notify_webhook", WEBHOOK)
        self.store.set_pref("notify_chat", True)
        dialog = self.dialog()
        self.store.set_pref("notify_chat", False)
        dialog._sync_chat_controls()
        self.assertFalse(self.store.get_pref("notify_chat"))

    def test_the_webhook_dialog_is_reached_and_the_tick_follows_it(self):
        dialog = self.dialog()

        def configure(*_args):
            self.store.set_pref("notify_webhook", WEBHOOK)
            return 1

        with patch("job_manager.chat_webhook_dialog.ChatWebhookDialog.exec", side_effect=configure):
            dialog.btn_chat.click()
        self.assertTrue(dialog.chk_chat.isEnabled())


class TestDesktop(SettingsTestCase):
    def test_the_badge_is_saved_and_redrawn_at_once(self):
        dialog = self.dialog()
        redrawn = MagicMock()
        self.service.jobs_changed.connect(redrawn)
        dialog.chk_taskbar_badge.setChecked(True)
        self.assertTrue(self.saved("taskbar_badge"))
        redrawn.assert_called()

    def test_switching_the_badge_off_clears_it_now(self):
        self.store.set_pref("taskbar_badge", True)
        dialog = self.dialog()
        with patch("job_manager.taskbar.clear_badge") as clear:
            dialog.chk_taskbar_badge.setChecked(False)
        clear.assert_called_once()

    def test_task_bar_progress_is_offered_only_where_it_exists(self):
        with patch("job_manager.win_taskbar.AVAILABLE", True):
            self.assertFalse(self.dialog().chk_taskbar_progress.isHidden())
        with patch("job_manager.win_taskbar.AVAILABLE", False):
            self.assertTrue(self.dialog().chk_taskbar_progress.isHidden())

    def test_task_bar_progress_is_saved(self):
        self.dialog().chk_taskbar_progress.setChecked(False)
        self.assertFalse(self.saved("taskbar_progress"))

    def test_keep_tracking_is_saved_and_applied_to_the_tray(self):
        tray = MagicMock()
        current = MagicMock(tray=tray)
        with patch("job_manager.presence.current", return_value=current):
            self.dialog().chk_keep_tracking.setChecked(True)
        self.assertTrue(self.saved("keep_running_in_tray"))
        tray.apply_keep_running.assert_called_once()

    def test_only_if_opened_is_on_and_follows_keep_tracking(self):
        self.assertTrue(JobStore(self.tmp).get_pref("keep_running_only_if_opened"))
        dialog = self.dialog()
        self.assertTrue(dialog.chk_only_if_opened.isChecked())
        self.assertFalse(dialog.chk_only_if_opened.isEnabled())
        with patch("job_manager.presence.current", return_value=None):
            dialog.chk_keep_tracking.setChecked(True)
        self.assertTrue(dialog.chk_only_if_opened.isEnabled())

    def test_only_if_opened_is_saved_and_applied_to_the_tray(self):
        tray = MagicMock()
        with patch("job_manager.presence.current", return_value=MagicMock(tray=tray)):
            self.dialog().chk_only_if_opened.setChecked(False)
        self.assertFalse(self.saved("keep_running_only_if_opened"))
        tray.apply_keep_running.assert_called_once()

    def test_keep_tracking_without_a_tray_is_still_saved(self):
        with patch("job_manager.presence.current", return_value=None):
            self.dialog().chk_keep_tracking.setChecked(True)
        self.assertTrue(self.saved("keep_running_in_tray"))


class TestStatusBarCounter(SettingsTestCase):
    def test_it_is_on_by_default_and_saved(self):
        dialog = self.dialog()
        self.assertTrue(dialog.chk_status_counter.isChecked())
        dialog.chk_status_counter.setChecked(False)
        self.assertFalse(self.saved("status_bar_counter"))

    def test_a_job_manager_on_its_own_has_no_status_bar_to_offer(self):
        self.assertTrue(self.dialog(standalone=True).chk_status_counter.isHidden())


class TestWebMonitor(SettingsTestCase):
    def setUp(self):
        super().setUp()
        from job_manager import web_service

        self.web = web_service.for_service(self.service)
        self.addCleanup(web_service.shutdown_for, self.service)

    def test_off_by_default(self):
        dialog = self.dialog()
        self.assertFalse(dialog.chk_web.isChecked())
        self.assertFalse(dialog.btn_web.isEnabled())

    def test_ticking_it_serves_and_names_the_port(self):
        dialog = self.dialog()
        dialog.chk_web.setChecked(True)
        self.assertTrue(self.web.running)
        self.assertTrue(self.saved("host_monitor_web"))
        self.assertIn(str(self.web.port), dialog.lbl_web.text())
        self.assertTrue(dialog.btn_web.isEnabled())

    def test_unticking_it_stops_and_is_remembered(self):
        self.web.start()
        dialog = self.dialog()
        self.assertTrue(dialog.chk_web.isChecked())
        dialog.chk_web.setChecked(False)
        self.assertFalse(self.web.running)
        self.assertFalse(self.saved("host_monitor_web"))

    def test_a_failed_start_is_shown_and_the_tick_comes_back_off(self):
        dialog = self.dialog()
        with (
            patch.object(self.web, "start", return_value=False),
            patch("job_manager.settings_dialog.QMessageBox.warning") as warn,
        ):
            dialog.chk_web.setChecked(True)
        warn.assert_called_once()
        self.assertFalse(dialog.chk_web.isChecked())

    def test_the_tray_process_offers_it_too(self):
        # It serves the page once MoleditPy has closed.
        titles = [g.title() for g in self.dialog(standalone=True).findChildren(QGroupBox)]
        self.assertIn("Web Monitor", titles)


class TestLocalApi(SettingsTestCase):
    def test_off_says_so(self):
        with (
            patch("job_manager.api_is_running", return_value=False),
            patch("job_manager.api_external_port", return_value=0),
        ):
            dialog = self.dialog()
        self.assertIn("Off", dialog.lbl_api.text())

    def test_on_names_the_port(self):
        server = MagicMock(port=8765)
        with (
            patch("job_manager.api_is_running", return_value=True),
            patch("job_manager.get_api_server", return_value=server),
        ):
            dialog = self.dialog()
        self.assertIn("8765", dialog.lbl_api.text())

    def test_served_elsewhere_says_where(self):
        with (
            patch("job_manager.api_is_running", return_value=False),
            patch("job_manager.api_external_port", return_value=9000),
        ):
            dialog = self.dialog()
        self.assertIn("9000", dialog.lbl_api.text())

    def test_the_button_opens_the_api_window(self):
        dialog = self.dialog()
        with patch("job_manager.api_dialog.ApiDialog.exec") as shown:
            dialog.btn_api.click()
        shown.assert_called_once()


class TestItIsReachable(SettingsTestCase):
    def test_from_the_plugin_menu(self):
        import job_manager

        with (
            patch.object(job_manager, "get_service", return_value=self.service),
            patch("job_manager.settings_dialog.SettingsDialog.exec") as shown,
        ):
            job_manager.show_settings(MagicMock())
        shown.assert_called_once()

    def test_a_failure_is_reported_not_raised(self):
        import job_manager

        context = MagicMock()
        with patch.object(job_manager, "get_service", side_effect=RuntimeError("no")):
            job_manager.show_settings(context)
        context.show_status_message.assert_called_once()

    def test_from_the_tray_process(self):
        from job_manager.standalone import StandaloneTray

        tray = StandaloneTray(self.app, self.service, self.tmp)
        with patch("job_manager.settings_dialog.SettingsDialog.exec") as shown:
            tray.open_settings()
        shown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
