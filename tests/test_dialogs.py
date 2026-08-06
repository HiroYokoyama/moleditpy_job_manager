"""Dialog construction and wiring, against real Qt widgets."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager.hosts_dialog import HostsDialog  # noqa: E402
from job_manager.jobs_dialog import (  # noqa: E402
    COLUMNS,
    JobsDialog,
    JobTableModel,
    format_duration,
    format_stamp,
    open_in_host,
    pick_primary_result,
)
from job_manager.models import (  # noqa: E402
    BACKEND_PARAMIKO,
    STATE_FAILED,
    STATE_RUNNING,
    Job,
)
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .fakes import FakeTransport, make_host, make_preset  # noqa: E402
from .test_poller import SyncPool  # noqa: E402


class DialogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dialogs_")
        self.store = JobStore(self.tmp)
        self.store.set_pref("download_root", os.path.join(self.tmp, "dl"))
        self.host = make_host()
        self.store.add_host(self.host)
        self.service = JobService(self.store)
        self.service.pool = SyncPool()
        self.service.poller.pool = SyncPool()
        self.transport = FakeTransport(self.host).when("sbatch", stdout="99\n")
        self.service.transport_for = lambda host: self.transport
        self.addCleanup(self.service.shutdown)

    def make_input(self, name="mol.inp"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        return path


class TestFormatting(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(format_duration(42), "42s")

    def test_minutes(self):
        self.assertEqual(format_duration(125), "2m 05s")

    def test_hours(self):
        self.assertEqual(format_duration(7325), "2h 02m")

    def test_negative_is_clamped(self):
        self.assertEqual(format_duration(-5), "0s")

    def test_missing_stamp(self):
        self.assertEqual(format_stamp(0), "-")

    def test_stamp_is_rendered(self):
        self.assertNotEqual(format_stamp(1_700_000_000), "-")


class TestPickPrimaryResult(unittest.TestCase):
    def test_prefers_out_over_log(self):
        self.assertEqual(pick_primary_result(["/d/a.log", "/d/a.out"]), "/d/a.out")

    def test_falls_back_to_log(self):
        self.assertEqual(pick_primary_result(["/d/a.log", "/d/a.tmp"]), "/d/a.log")

    def test_falls_back_to_the_first_file(self):
        self.assertEqual(pick_primary_result(["/d/a.tmp"]), "/d/a.tmp")

    def test_empty(self):
        self.assertEqual(pick_primary_result([]), "")


class TestOpenInHost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="openhost_")
        self.path = os.path.join(self.tmp, "mol.out")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("x")

    def test_no_context_returns_false(self):
        with patch("job_manager.get_context", return_value=None):
            self.assertFalse(open_in_host(self.path))

    def test_missing_file_returns_false(self):
        with patch("job_manager.get_context", return_value=MagicMock()):
            self.assertFalse(open_in_host(os.path.join(self.tmp, "nope.out")))

    def test_uses_the_host_command_line_loader(self):
        context = MagicMock()
        loader = context.get_main_window.return_value.init_manager.load_command_line_file
        with patch("job_manager.get_context", return_value=context):
            self.assertTrue(open_in_host(self.path))
        loader.assert_called_once_with(self.path)

    def test_falls_back_to_a_registered_plugin_opener(self):
        context = MagicMock()
        main_window = context.get_main_window.return_value
        main_window.init_manager = None
        callback = MagicMock()
        main_window.plugin_manager.file_openers = {".out": [{"callback": callback}]}
        with patch("job_manager.get_context", return_value=context):
            self.assertTrue(open_in_host(self.path))
        callback.assert_called_once_with(self.path)

    def test_a_failing_loader_is_contained(self):
        context = MagicMock()
        context.get_main_window.return_value.init_manager.load_command_line_file.side_effect = (
            RuntimeError("bad file")
        )
        with patch("job_manager.get_context", return_value=context):
            self.assertFalse(open_in_host(self.path))

    def test_no_opener_for_the_extension(self):
        context = MagicMock()
        main_window = context.get_main_window.return_value
        main_window.init_manager = None
        main_window.plugin_manager.file_openers = {}
        with patch("job_manager.get_context", return_value=context):
            self.assertFalse(open_in_host(self.path))


class TestJobTableModel(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(
            Job(
                id="j1",
                name="alpha",
                host_name="hpc",
                remote_job_id="7",
                state=STATE_RUNNING,
                submitted_at=1000.0,
            )
        )
        self.model = JobTableModel(self.service)

    def cell(self, row, column):
        from PyQt6.QtCore import Qt

        return self.model.data(self.model.index(row, column), Qt.ItemDataRole.DisplayRole)

    def test_dimensions(self):
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.columnCount(), len(COLUMNS))

    def test_headers(self):
        from PyQt6.QtCore import Qt

        self.assertEqual(
            self.model.headerData(0, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole),
            "Name",
        )

    def test_cells(self):
        self.assertEqual(self.cell(0, 0), "alpha")
        self.assertEqual(self.cell(0, 1), "hpc")
        self.assertEqual(self.cell(0, 2), "7")
        self.assertEqual(self.cell(0, 3), STATE_RUNNING)

    def test_missing_queue_id_shows_a_dash(self):
        self.store.add_job(Job(id="j2", submitted_at=2000.0))
        self.model.reload()
        self.assertEqual(self.cell(0, 2), "-")

    def test_failed_state_shows_the_exit_code(self):
        self.store.jobs["j1"].state = STATE_FAILED
        self.store.jobs["j1"].rc = 137
        self.assertIn("137", self.cell(0, 3))

    def test_tooltip_mentions_the_remote_directory(self):
        from PyQt6.QtCore import Qt

        self.store.jobs["j1"].remote_dir = "/scratch/j1"
        tip = self.model.data(self.model.index(0, 0), Qt.ItemDataRole.ToolTipRole)
        self.assertIn("/scratch/j1", tip)

    def test_row_lookup(self):
        self.assertEqual(self.model.row_of("j1"), 0)
        self.assertEqual(self.model.row_of("ghost"), -1)

    def test_job_at_out_of_range(self):
        self.assertIsNone(self.model.job_at(99))

    def test_refresh_of_an_unknown_job_reloads(self):
        self.store.add_job(Job(id="j2", submitted_at=3000.0))
        self.model.refresh_job("j2")
        self.assertEqual(self.model.rowCount(), 2)


class TestJobsDialog(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_interval_spinbox_reflects_the_store(self):
        self.assertEqual(self.dialog.spin_interval.value(), self.store.poll_interval)

    def test_interval_floor_is_enforced_by_the_widget(self):
        self.dialog.spin_interval.setValue(1)
        self.assertGreaterEqual(self.dialog.spin_interval.value(), 30)

    def test_changing_the_interval_persists_and_reschedules(self):
        self.dialog.spin_interval.setValue(300)
        self.assertEqual(JobStore(self.tmp).get_pref("poll_interval"), 300)

    def test_buttons_are_disabled_without_a_selection(self):
        self.assertFalse(self.dialog.btn_cancel.isEnabled())
        self.assertFalse(self.dialog.btn_download.isEnabled())

    def test_selecting_an_active_job_enables_cancel(self):
        self.service.submit(self.host, make_preset(), "mol", [self.make_input()])
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        self.assertTrue(self.dialog.btn_cancel.isEnabled())

    def test_open_result_needs_downloaded_files(self):
        job = self.service.submit(self.host, make_preset(), "mol", [self.make_input()])
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        self.assertFalse(self.dialog.btn_open.isEnabled())
        job.downloaded_files = ["/tmp/mol.out"]
        self.dialog._update_buttons()
        self.assertTrue(self.dialog.btn_open.isEnabled())

    def test_service_messages_reach_the_log_pane(self):
        self.service.message.emit("hello from the cluster")
        self.assertIn("hello from the cluster", self.dialog.txt_log.toPlainText())

    def test_tail_output_replaces_the_log_pane(self):
        self.service.log_ready.emit("SCF converged")
        self.assertEqual(self.dialog.txt_log.toPlainText(), "SCF converged")

    def test_rate_limited_refresh_is_explained(self):
        self.service.submit(self.host, make_preset(), "mol", [self.make_input()])
        self.transport.when("squeue", stdout="")
        self.dialog._refresh_now()
        self.dialog._refresh_now()
        self.assertIn("rate limited", self.dialog.txt_log.toPlainText())

    def test_auto_open_preference_is_persisted(self):
        self.dialog.chk_auto_open.setChecked(False)
        self.assertFalse(JobStore(self.tmp).get_pref("open_result_after_download"))

    def test_results_ready_does_not_open_when_auto_open_is_off(self):
        self.dialog.chk_auto_open.setChecked(False)
        with patch("job_manager.jobs_dialog.open_in_host") as opener:
            self.service.results_ready.emit("j1", ["/tmp/a.out"])
        opener.assert_not_called()

    def test_results_ready_opens_when_auto_open_is_on(self):
        self.dialog.chk_auto_open.setChecked(True)
        with patch("job_manager.jobs_dialog.open_in_host", return_value=True) as opener:
            self.service.results_ready.emit("j1", ["/tmp/a.out"])
        opener.assert_called_once_with("/tmp/a.out")

    def test_submit_without_a_host_offers_the_hosts_dialog(self):
        self.store.hosts.clear()
        with patch("job_manager.jobs_dialog.QMessageBox.information") as info:
            with patch.object(JobsDialog, "open_hosts_dialog") as hosts:
                self.dialog.open_submit_dialog()
        info.assert_called_once()
        hosts.assert_called_once()

    def test_remove_asks_first(self):
        from PyQt6.QtWidgets import QMessageBox

        self.service.submit(self.host, make_preset(), "mol", [self.make_input()])
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.dialog._remove_selected()
        self.assertEqual(len(self.store.jobs), 1)

    def test_remove_deletes_when_confirmed(self):
        from PyQt6.QtWidgets import QMessageBox

        self.service.submit(self.host, make_preset(), "mol", [self.make_input()])
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._remove_selected()
        self.assertEqual(self.store.jobs, {})

    def test_actions_without_a_selection_are_no_ops(self):
        self.dialog._cancel_selected()
        self.dialog._download_selected()
        self.dialog._tail_selected()
        self.dialog._remove_selected()
        self.dialog._open_selected_result()

    def test_close_deregisters_the_window(self):
        with patch("job_manager.forget_window") as forget:
            self.dialog.close()
        forget.assert_called_once()

    def test_closing_does_not_stop_polling(self):
        self.service.submit(self.host, make_preset(), "mol", [self.make_input()])
        with patch("job_manager.forget_window"):
            self.dialog.close()
        self.assertTrue(self.service.poller.timer.isActive())


class TestHostsDialog(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = HostsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_existing_hosts_are_listed(self):
        self.assertEqual(self.dialog.list.count(), 1)

    def test_the_form_loads_the_selection(self):
        self.assertEqual(self.dialog.txt_hostname.text(), "login.example.org")

    def test_editing_and_saving_persists(self):
        self.dialog.txt_hostname.setText("new.example.org")
        self.dialog._save_current()
        self.assertEqual(JobStore(self.tmp).hosts[self.host.id].hostname, "new.example.org")

    def test_adding_a_host(self):
        self.dialog._add_host()
        self.assertEqual(len(self.store.hosts), 2)

    def test_removing_a_host_asks_first(self):
        from PyQt6.QtWidgets import QMessageBox

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.dialog._remove_host()
        self.assertEqual(len(self.store.hosts), 1)

    def test_removing_a_host_when_confirmed(self):
        from PyQt6.QtWidgets import QMessageBox

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._remove_host()
        self.assertEqual(self.store.hosts, {})

    def test_openssh_hint_warns_about_batch_mode(self):
        self.dialog.cmb_backend.setCurrentIndex(0)
        self.assertIn("Batch mode", self.dialog.lbl_backend_hint.text())

    def test_paramiko_hint_when_missing(self):
        index = self.dialog.cmb_backend.findData(BACKEND_PARAMIKO)
        with patch("job_manager.hosts_dialog.paramiko_available", return_value=False):
            self.dialog.cmb_backend.setCurrentIndex(index)
            self.dialog._update_backend_hint()
        self.assertIn("pip install paramiko", self.dialog.lbl_backend_hint.text())

    def test_paramiko_hint_promises_no_disk_storage(self):
        index = self.dialog.cmb_backend.findData(BACKEND_PARAMIKO)
        with patch("job_manager.hosts_dialog.paramiko_available", return_value=True):
            self.dialog.cmb_backend.setCurrentIndex(index)
            self.dialog._update_backend_hint()
        self.assertIn("never written to disk", self.dialog.lbl_backend_hint.text())

    def test_test_connection_reports_success(self):
        self.transport.when("hostname", stdout="moleditpy_ok\nnode01\n")
        self.dialog._test_connection()
        self.assertIn("node01", self.dialog.lbl_test.text())

    def test_test_connection_without_a_hostname(self):
        self.dialog.txt_hostname.setText("")
        self.dialog._test_connection()
        self.assertIn("hostname", self.dialog.lbl_test.text())

    def test_test_connection_reports_failure(self):
        def exploding(host):
            raise RuntimeError("no route to host")

        self.service.transport_for = exploding
        self.dialog._test_connection()
        self.assertIn("no route", self.dialog.lbl_test.text())

    def test_blank_name_falls_back(self):
        self.dialog.txt_name.setText("")
        host = self.dialog._save_current()
        self.assertEqual(host.name, "cluster")

    def test_login_commands_are_split_into_lines(self):
        self.dialog.txt_login.setPlainText("source /etc/profile\n\nmodule purge\n")
        host = self.dialog._save_current()
        self.assertEqual(host.login_commands, ["source /etc/profile", "module purge"])


class TestSubmitDialog(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = SubmitDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_hosts_are_offered(self):
        self.assertEqual(self.dialog.cmb_host.count(), 1)
        self.assertIs(self.dialog.current_host(), self.host)

    def test_the_preview_shows_the_real_script(self):
        text = self.dialog.txt_preview.toPlainText()
        self.assertTrue(text.startswith("#!/bin/bash"))
        self.assertIn("#SBATCH", text)
        self.assertIn(".moleditpy_rc", text)

    def test_the_preview_follows_the_command_field(self):
        self.dialog.txt_command.setText("g16 {input}")
        self.assertIn("g16 input.inp", self.dialog.txt_preview.toPlainText())

    def test_the_preview_follows_the_resources(self):
        self.dialog.txt_walltime.setText("99:00:00")
        self.assertIn("99:00:00", self.dialog.txt_preview.toPlainText())

    def test_saving_a_preset(self):
        self.dialog.txt_job_name.setText("orca opt")
        self.dialog.txt_queue.setText("short")
        self.dialog._save_preset()
        presets = self.store.presets_for_host(self.host.id)
        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0].queue, "short")

    def test_reloading_a_preset_repopulates_the_form(self):
        self.store.add_preset(make_preset(queue="gpu", memory="64G"))
        self.dialog._reload_hosts()
        self.assertEqual(self.dialog.txt_queue.text(), "gpu")
        self.assertEqual(self.dialog.txt_memory.text(), "64G")

    def test_submit_requires_an_input_file(self):
        with patch("job_manager.submit_dialog.QMessageBox.warning") as warn:
            self.dialog._submit()
        warn.assert_called_once()
        self.assertEqual(self.store.jobs, {})

    def test_submit_rejects_a_missing_file(self):
        self.dialog.list_files.addItem(os.path.join(self.tmp, "ghost.inp"))
        with patch("job_manager.submit_dialog.QMessageBox.warning") as warn:
            self.dialog._submit()
        warn.assert_called_once()

    def test_submit_requires_a_command(self):
        self.dialog.list_files.addItem(self.make_input())
        self.dialog.txt_command.setText("   ")
        with patch("job_manager.submit_dialog.QMessageBox.warning") as warn:
            self.dialog._submit()
        warn.assert_called_once()

    def test_a_valid_submission_creates_a_job(self):
        self.dialog.list_files.addItem(self.make_input())
        self.dialog.txt_job_name.setText("opt")
        self.dialog._submit()
        self.assertEqual(len(self.store.jobs), 1)

    def test_fetch_patterns_are_parsed_from_the_field(self):
        self.dialog.txt_globs.setText("*.out, *.gbw ,")
        self.assertEqual(self.dialog.collect_preset().fetch_globs, ["*.out", "*.gbw"])

    def test_removing_a_file_from_the_list(self):
        self.dialog.list_files.addItem(self.make_input())
        self.dialog.list_files.setCurrentRow(0)
        self.dialog._remove_file()
        self.assertEqual(self.dialog.selected_files(), [])

    def test_no_host_shows_a_hint_in_the_preview(self):
        self.store.hosts.clear()
        self.dialog._reload_hosts()
        self.assertIn("host profile", self.dialog.txt_preview.toPlainText())

    def test_submit_without_a_host_warns(self):
        self.store.hosts.clear()
        self.dialog._reload_hosts()
        with patch("job_manager.submit_dialog.QMessageBox.warning") as warn:
            self.dialog._submit()
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
