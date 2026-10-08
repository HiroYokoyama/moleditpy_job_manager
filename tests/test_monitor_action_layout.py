"""Compact monitor controls and shared action state during asynchronous work."""

from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QAbstractButton, QApplication  # noqa: E402

from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import Job, STATE_DONE  # noqa: E402

from .test_dialogs import DialogTestCase  # noqa: E402
from .test_host_monitor_gui import HostMonitorTestCase  # noqa: E402


class TestJobActions(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(
            Job(id="first", host_id=self.host.id, remote_dir="~/jobs/first", state=STATE_DONE)
        )
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.close)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.table.selectRow(0)

    def test_selection_changes_do_not_restart_a_pending_listing(self):
        with patch.object(self.service, "list_remote_results") as listing:
            self.dialog.btn_download.click()
        self.store.add_job(
            Job(id="second", host_id=self.host.id, remote_dir="~/jobs/second", state=STATE_DONE)
        )
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        self.assertFalse(self.dialog.btn_download.isEnabled())
        self.dialog.job_actions["download"].trigger()
        self.assertEqual(listing.call_count, 1)
        listing.call_args.args[2]("Could not list files")
        self.assertTrue(self.dialog.btn_download.isEnabled())

    def test_finishing_a_listing_respects_the_current_selection(self):
        with patch.object(self.service, "list_remote_results") as listing:
            self.dialog.btn_download.click()
        self.dialog.table.clearSelection()
        listing.call_args.args[2]("Could not list files")
        self.assertFalse(self.dialog.btn_download.isEnabled())
        self.assertFalse(self.dialog.job_actions["download"].isEnabled())

    def test_the_window_fits_a_narrow_width_with_secondary_actions_in_menus(self):
        self.dialog.resize(640, 480)
        self.dialog.show()
        QApplication.processEvents()
        self.assertEqual(self.dialog.width(), 640)
        self.assertGreaterEqual(self.dialog.table.horizontalHeader().sectionSize(0), 180)
        visible = [
            b for b in self.dialog.findChildren(QAbstractButton) if b.isVisibleTo(self.dialog)
        ]
        self.assertLessEqual(len(visible), 8)
        for button in visible:
            self.assertTrue(
                self.dialog.rect().contains(button.mapTo(self.dialog, button.rect().topLeft()))
            )
            self.assertTrue(
                self.dialog.rect().contains(button.mapTo(self.dialog, button.rect().bottomRight()))
            )
        self.assertIn(self.dialog.job_actions["remove"], self.dialog.menu_job.actions())

    def test_the_new_job_shortcut_does_not_pass_a_checkbox_value_as_files(self):
        self.dialog.show()
        self.dialog.activateWindow()
        QApplication.processEvents()
        with patch("job_manager.submit_dialog.SubmitDialog") as submit:
            QTest.keyClick(self.dialog, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
        submit.assert_called_once()
        submit.return_value.prefill.assert_not_called()
        submit.return_value.exec.assert_called_once()


class TestHostViewActions(HostMonitorTestCase):
    def test_view_menu_toggles_the_graphs_and_saves_the_choice(self):
        dialog = self.monitor()
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.cards[self.host.id].expanded)
        dialog.action_history.trigger()
        self.assertTrue(dialog.cards[self.host.id].expanded)
        self.assertTrue(self.store.get_pref("host_monitor_history"))
        dialog.action_dark.trigger()
        self.assertTrue(self.store.get_pref("host_monitor_dark"))

    def test_refresh_button_requests_a_sample_and_close_shortcut_stops_the_monitor(self):
        dialog = self.monitor()
        self.addCleanup(dialog.close)
        initial = self.transports[self.host.id].runs
        dialog.btn_refresh.click()
        self.assertGreater(self.transports[self.host.id].runs, initial)
        dialog.show()
        dialog.activateWindow()
        QApplication.processEvents()
        QTest.keyClick(dialog, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(dialog._torn_down)
        self.assertFalse(dialog.isVisible())
