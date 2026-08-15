"""Dialog construction and wiring, against real Qt widgets."""

import json
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
from PyQt6.QtWidgets import QMessageBox  # noqa: E402
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
        self.dialog.spin_interval.setValue(0)
        self.assertGreaterEqual(self.dialog.spin_interval.value(), 5)

    def test_a_fast_interval_is_accepted_but_flagged(self):
        self.dialog.spin_interval.setValue(10)
        self.assertEqual(self.store.poll_interval, 10)
        self.assertTrue(self.dialog.lbl_interval_warning.text())
        self.assertIn("login node", self.dialog.lbl_interval_warning.toolTip())

    def test_the_warning_clears_when_the_interval_is_courteous(self):
        self.dialog.spin_interval.setValue(10)
        self.dialog.spin_interval.setValue(120)
        self.assertEqual(self.dialog.lbl_interval_warning.text(), "")

    def test_no_warning_at_the_default_interval(self):
        self.assertEqual(self.dialog.lbl_interval_warning.text(), "")

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

    def test_the_resource_budgets_are_typed_not_detected(self):
        # What a shared login node reports is the whole machine, so the number
        # the user knows is the honest default.
        self.dialog._add_host()
        self.assertFalse(self.dialog.chk_detect_resources.isChecked())
        self.assertEqual(self.dialog.spin_runner_cores.minimum(), 1)
        self.dialog.spin_runner_cores.setValue(16)
        self.dialog.spin_runner_memory.setValue(64)
        host = self.dialog._save_current()
        self.assertEqual((host.runner_cores, host.runner_memory_mb), (16, 64 * 1024))
        self.assertFalse(host.runner_detect)

    def test_ticking_detect_hands_the_budget_to_the_host(self):
        self.dialog.chk_detect_resources.setChecked(True)
        host = self.dialog._save_current()
        # 0 is the protocol's own "read the machine", so nothing is invented.
        self.assertEqual((host.runner_cores, host.runner_memory_mb), (0, 0))
        self.assertTrue(host.runner_detect)
        self.assertFalse(self.dialog.spin_runner_cores.isEnabled())

    def test_a_new_host_reads_the_login_files(self):
        self.dialog._add_host()
        self.assertTrue(self.dialog.chk_load_profile.isChecked())
        self.assertTrue(self.dialog._save_current().load_profile)

    def test_the_password_box_is_dead_outside_paramiko(self):
        from job_manager.models import BACKEND_OPENSSH

        # OpenSSH runs in batch mode and cannot do password authentication.
        self.dialog.cmb_backend.setCurrentIndex(self.dialog.cmb_backend.findData(BACKEND_OPENSSH))
        self.assertFalse(self.dialog.chk_ask_password.isEnabled())
        self.dialog.cmb_backend.setCurrentIndex(self.dialog.cmb_backend.findData(BACKEND_PARAMIKO))
        self.assertTrue(self.dialog.chk_ask_password.isEnabled())

    def test_the_editing_column_scrolls(self):
        from PyQt6.QtWidgets import QDialogButtonBox, QScrollArea

        scroll = self.dialog.findChild(QScrollArea)
        self.assertIsNotNone(scroll)
        self.assertTrue(scroll.widgetResizable())
        # Save and Close must not scroll away with the form.
        box = self.dialog.findChild(QDialogButtonBox)
        self.assertNotIn(box, scroll.widget().findChildren(QDialogButtonBox))

    def test_saving_says_so(self):
        self.dialog._save_current()
        self.assertIn("Saved", self.dialog.lbl_test.text())

    def test_the_form_is_dead_with_nothing_selected(self):
        from PyQt6.QtWidgets import QMessageBox

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._remove_host()
        self.assertEqual(self.dialog.list.count(), 0)
        # Otherwise a whole profile can be typed in with nowhere for it to go,
        # and Save and Test Connection both quietly do nothing.
        self.assertFalse(self.dialog.form_box.isEnabled())
        self.assertFalse(self.dialog.adv_box.isEnabled())
        self.assertFalse(self.dialog.btn_save.isEnabled())
        self.assertFalse(self.dialog.btn_test.isEnabled())
        self.assertIn("Add", self.dialog.lbl_test.text())

    def test_the_form_comes_back_with_a_new_host(self):
        from PyQt6.QtWidgets import QMessageBox

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._remove_host()
        self.dialog._add_host()
        self.assertTrue(self.dialog.form_box.isEnabled())
        self.assertTrue(self.dialog.btn_save.isEnabled())


class TestHostEnabledAndEqualPath(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = HostsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_a_loaded_host_shows_enabled(self):
        self.assertTrue(self.dialog.chk_enabled.isChecked())

    def test_unchecking_and_saving_disables_the_host(self):
        self.dialog.chk_enabled.setChecked(False)
        host = self.dialog._save_current()
        self.assertFalse(host.enabled)
        self.assertFalse(JobStore(self.tmp).hosts[self.host.id].enabled)

    def test_a_disabled_host_is_marked_in_the_list(self):
        self.dialog.chk_enabled.setChecked(False)
        self.dialog._save_current()
        self.assertIn("[disabled]", self.dialog.list.item(0).text())

    def test_equal_path_round_trips(self):
        self.dialog.txt_equal_path.setText("/mnt/cluster")
        host = self.dialog._save_current()
        self.assertEqual(host.equal_path, "/mnt/cluster")
        self.assertEqual(JobStore(self.tmp).hosts[self.host.id].equal_path, "/mnt/cluster")

    def test_equal_path_is_disabled_for_a_local_host(self):
        from job_manager.models import BACKEND_LOCAL

        index = self.dialog.cmb_backend.findData(BACKEND_LOCAL)
        self.dialog.cmb_backend.setCurrentIndex(index)
        self.assertFalse(self.dialog.txt_equal_path.isEnabled())

    def test_equal_path_is_enabled_for_a_remote_host(self):
        self.assertTrue(self.dialog.txt_equal_path.isEnabled())


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

    def test_the_body_scrolls(self):
        from PyQt6.QtWidgets import QDialogButtonBox, QScrollArea

        scroll = self.dialog.findChild(QScrollArea)
        self.assertIsNotNone(scroll)
        self.assertTrue(scroll.widgetResizable())
        self.assertIn(
            self.dialog.txt_preview, scroll.widget().findChildren(type(self.dialog.txt_preview))
        )
        # Submit must not scroll away with the rest of it.
        box = self.dialog.findChild(QDialogButtonBox)
        self.assertNotIn(box, scroll.widget().findChildren(QDialogButtonBox))

    def test_the_command_is_visible_without_opening_a_tab(self):
        from PyQt6.QtWidgets import QTabWidget

        # A job is a command; behind a tab of queue settings the wizard read as
        # though a job were an input file and nothing else.
        tabs = self.dialog.findChild(QTabWidget)
        self.assertNotIn(self.dialog.txt_command, tabs.findChildren(type(self.dialog.txt_command)))
        self.assertTrue(self.dialog.txt_command.isVisibleTo(self.dialog))

    def test_the_file_list_says_it_is_optional(self):
        from PyQt6.QtWidgets import QGroupBox

        titles = [box.title() for box in self.dialog.findChildren(QGroupBox)]
        self.assertTrue(any("optional" in title for title in titles if title.startswith("Input")))

    def test_save_as_preset_has_a_line_of_its_own(self):
        from PyQt6.QtWidgets import QDialogButtonBox, QPushButton, QScrollArea

        scroll = self.dialog.findChild(QScrollArea)
        box = self.dialog.findChild(QDialogButtonBox)
        # Pinned like Submit rather than scrolling with the body, and not on the
        # same row as it: saving a preset is not submitting.
        self.assertNotIn(self.dialog.btn_save_preset, scroll.widget().findChildren(QPushButton))
        self.assertNotIn(self.dialog.btn_save_preset, box.buttons())

    def test_it_fits_on_a_short_screen(self):
        self.assertLessEqual(self.dialog._preferred_height(4000), 4000)
        self.assertGreaterEqual(self.dialog._preferred_height(640), 400)

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

    def test_submit_with_no_input_at_all_asks_first(self):
        # Legal -- a command need not have an input file -- but nearly always
        # a forgotten one, so it is confirmed rather than warned about.
        self.dialog.txt_command.setText("./run_all.sh")
        with patch(
            "job_manager.submit_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as ask:
            self.dialog._submit()
        ask.assert_called_once()
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


class TestTheWorkAlreadyOnTheHostBox(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = SubmitDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.transport.when("PRESENT", stdout="PRESENT\n")

    def use_remote(self, path="~/runs/mol42", input_name=""):
        self.dialog.box_remote.setChecked(True)
        self.dialog.txt_remote_dir.setText(path)
        self.dialog.txt_remote_input.setText(input_name)

    def test_it_is_off_until_asked_for(self):
        self.assertFalse(self.dialog.box_remote.isChecked())
        self.assertEqual(self.dialog.remote_dir(), "")

    def test_unticking_the_box_puts_the_job_back_in_its_own_directory(self):
        self.use_remote()
        self.dialog.box_remote.setChecked(False)
        self.assertEqual(self.dialog.remote_dir(), "")
        self.assertIn("moleditpy_jobs", self.dialog.txt_preview.toPlainText())

    def test_the_preview_cds_into_that_directory(self):
        self.use_remote()
        self.assertIn("cd ~/runs/mol42", self.dialog.txt_preview.toPlainText())

    def test_the_preview_uses_the_input_that_is_already_there(self):
        self.dialog.txt_command.setText("orca {input} > {stem}.out")
        self.use_remote(input_name="staged.inp")
        self.assertIn("orca staged.inp > staged.out", self.dialog.txt_preview.toPlainText())

    def test_submitting_with_no_local_files_at_all(self):
        self.use_remote(input_name="staged.inp")
        self.dialog.txt_job_name.setText("staged")
        self.dialog._submit()
        job = list(self.store.jobs.values())[0]
        self.assertEqual(job.remote_dir, "~/runs/mol42")
        self.assertEqual(job.remote_input, "staged.inp")
        self.assertEqual(job.input_files, [])

    def test_a_command_that_still_names_an_input_is_refused(self):
        # The default template is `orca {input} > {stem}.out`, which with no
        # input at all would run as `orca  > .out` and fail on the host.
        self.use_remote()
        with patch("job_manager.submit_dialog.QMessageBox.warning") as warn:
            self.dialog._submit()
        warn.assert_called_once()
        self.assertIn("{input}", warn.call_args.args[2])
        self.assertEqual(self.store.jobs, {})

    def test_naming_the_input_on_the_host_satisfies_it(self):
        self.use_remote(input_name="staged.inp")
        self.dialog._submit()
        self.assertEqual(len(self.store.jobs), 1)

    def test_a_command_of_its_own_needs_no_input(self):
        self.use_remote()
        self.dialog.txt_command.setText("./run_all.sh")
        self.dialog._submit()
        self.assertEqual(len(self.store.jobs), 1)

    def test_a_ticked_box_with_no_directory_is_refused(self):
        self.dialog.box_remote.setChecked(True)
        with patch("job_manager.submit_dialog.QMessageBox.warning") as warn:
            self.dialog._submit()
        warn.assert_called_once()
        self.assertEqual(self.store.jobs, {})

    def test_confirming_a_job_with_no_input_at_all_submits_it(self):
        self.dialog.txt_command.setText("./run_all.sh")
        with patch(
            "job_manager.submit_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.dialog._submit()
        self.assertEqual(len(self.store.jobs), 1)

    def test_check_reports_what_is_in_the_directory(self):
        self.transport.when("ls -p", stdout="a.out\nb.out\n")
        self.use_remote()
        self.dialog._check_remote_dir()
        self.assertIn("2 file(s)", self.dialog.lbl_remote.text())

    def test_check_says_so_when_the_named_input_is_not_there(self):
        self.transport.when("ls -p", stdout="a.out\n")
        self.use_remote(input_name="staged.inp")
        self.dialog._check_remote_dir()
        self.assertIn("staged.inp", self.dialog.lbl_remote.text())

    def test_check_reports_a_directory_that_is_not_there(self):
        self.transport.clear_rules()
        self.use_remote()
        self.dialog._check_remote_dir()
        self.assertIn("~/runs/mol42", self.dialog.lbl_remote.text())
        self.assertTrue(self.dialog.btn_check_remote.isEnabled())


class TestPrefillAndResubmit(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def submitted_job(self, **preset_kwargs):
        preset = make_preset(**preset_kwargs)
        return self.service.submit(self.host, preset, "mol", [self.make_input()])

    def test_the_preset_is_snapshotted_onto_the_job(self):
        job = self.submitted_job(queue="gpu", memory="64G")
        self.assertEqual(job.preset["queue"], "gpu")
        self.assertEqual(job.preset["memory"], "64G")

    def test_the_snapshot_survives_a_restart(self):
        job = self.submitted_job(queue="gpu")
        self.assertEqual(JobStore(self.tmp).jobs[job.id].preset["queue"], "gpu")

    def test_the_snapshot_outlives_the_named_preset(self):
        # Resubmit must still work after the preset is edited or deleted.
        preset = make_preset(queue="gpu")
        self.store.add_preset(preset)
        job = self.service.submit(self.host, preset, "mol", [self.make_input()])
        self.store.remove_preset(preset.id)
        self.assertEqual(job.preset["queue"], "gpu")

    def test_resubmit_prefills_the_wizard(self):
        job = self.submitted_job(queue="gpu")
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        with patch.object(JobsDialog, "open_submit_dialog") as opener:
            self.dialog._resubmit_selected()
        kwargs = opener.call_args.kwargs
        self.assertEqual(kwargs["files"], job.input_files)
        self.assertEqual(kwargs["host_id"], self.host.id)
        self.assertEqual(kwargs["preset"]["queue"], "gpu")
        self.assertEqual(kwargs["name"], job.name)

    def test_resubmit_keeps_a_job_pointed_at_the_directory_it_ran_in(self):
        self.transport.when("PRESENT", stdout="PRESENT\n")
        job = self.service.submit(
            self.host,
            make_preset(),
            "staged",
            [],
            remote_dir="~/runs/mol42",
            remote_input="staged.inp",
        )
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        with patch.object(JobsDialog, "open_submit_dialog") as opener:
            self.dialog._resubmit_selected()
        kwargs = opener.call_args.kwargs
        self.assertEqual(kwargs["remote_dir"], job.remote_dir)
        self.assertEqual(kwargs["remote_input"], "staged.inp")

    def test_a_command_only_job_can_be_resubmitted_at_all(self):
        self.transport.when("PRESENT", stdout="PRESENT\n")
        self.service.submit(self.host, make_preset(), "staged", [], remote_dir="~/runs/mol42")
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        self.dialog._update_buttons()
        self.assertTrue(self.dialog.btn_resubmit.isEnabled())

    def test_the_wizard_opens_with_the_box_already_ticked(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.prefill(remote_dir="~/runs/mol42", remote_input="staged.inp")
        self.assertTrue(dialog.box_remote.isChecked())
        self.assertEqual(dialog.remote_dir(), "~/runs/mol42")
        self.assertEqual(dialog.remote_input(), "staged.inp")
        self.assertEqual(dialog.txt_job_name.text(), "staged")

    def test_resubmit_refuses_when_the_input_is_gone(self):
        job = self.submitted_job()
        os.unlink(job.input_files[0])
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        with patch("job_manager.jobs_dialog.QMessageBox.warning") as warn:
            with patch.object(JobsDialog, "open_submit_dialog") as opener:
                self.dialog._resubmit_selected()
        warn.assert_called_once()
        opener.assert_not_called()

    def test_resubmit_is_enabled_only_with_input_files(self):
        self.submitted_job()
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        self.assertTrue(self.dialog.btn_resubmit.isEnabled())

    def test_resubmit_without_a_selection_is_a_no_op(self):
        self.dialog._resubmit_selected()

    def test_prefill_populates_the_submit_dialog(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        path = self.make_input("prefilled.inp")
        dialog.prefill(
            files=[path],
            host_id=self.host.id,
            preset=make_preset(queue="short", memory="8G").to_dict(),
        )
        self.assertEqual(dialog.selected_files(), [path])
        self.assertEqual(dialog.txt_job_name.text(), "prefilled")
        self.assertEqual(dialog.txt_queue.text(), "short")
        self.assertIn("#SBATCH --mem=8G", dialog.txt_preview.toPlainText())

    def test_prefill_replaces_rather_than_appends(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.list_files.addItem(self.make_input("stale.inp"))
        fresh = self.make_input("fresh.inp")
        dialog.prefill(files=[fresh])
        self.assertEqual(dialog.selected_files(), [fresh])

    def test_an_explicit_name_wins_over_the_file_stem(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.prefill(files=[self.make_input("a.inp")], name="my run")
        self.assertEqual(dialog.txt_job_name.text(), "my run")

    def test_prefill_remembers_the_directory(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        path = self.make_input("remembered.inp")
        dialog.prefill(files=[path])
        self.assertEqual(self.store.get_pref("last_input_dir"), os.path.dirname(path))

    def test_prefill_with_nothing_is_harmless(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.prefill()
        self.assertEqual(dialog.selected_files(), [])


class TestTheMonitorReleasesTheServiceOnClose(DialogTestCase):
    """The service outlives the window, so a leftover connection is forever.

    Every open/close cycle used to leave a live subscriber behind: a finished
    job's results were handed to the host application once per window the user
    had ever opened, and each closed window still reloaded its model on every
    poll.
    """

    def test_closing_drops_every_connection(self):
        before = self.service.receivers(self.service.jobs_changed)
        dialog = JobsDialog(self.service)
        self.assertGreater(self.service.receivers(self.service.jobs_changed), before)
        dialog.close()
        self.assertEqual(self.service.receivers(self.service.jobs_changed), before)

    def test_results_are_opened_once_not_once_per_window_ever_opened(self):
        opened = []
        with patch("job_manager.jobs_dialog.open_in_host", side_effect=lambda p: opened.append(p)):
            for _ in range(3):
                JobsDialog(self.service).close()
            live = JobsDialog(self.service)
            self.addCleanup(live.close)
            self.service.results_ready.emit("job1", [os.path.join(self.tmp, "result.out")])
        self.assertEqual(len(opened), 1, f"opened {len(opened)} times")

    def test_a_closed_window_stops_reloading_its_model(self):
        dialog = JobsDialog(self.service)
        reloads = []
        dialog.model.reload = lambda: reloads.append(1)
        dialog.close()
        self.service.jobs_changed.emit()
        self.assertEqual(reloads, [])

    def test_closing_twice_is_harmless(self):
        dialog = JobsDialog(self.service)
        dialog.close()
        dialog.close()


class TestResubmitWhenTheHostProfileIsGone(DialogTestCase):
    """Prefill cannot select a host that no longer exists, so the wizard would
    silently open on whichever host sorted first."""

    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.close)
        self.job = Job(name="orphan", host_id="retired-id", host_name="retired cluster")
        self.job.input_files = [self.make_input()]
        self.store.add_job(self.job)
        self.dialog.model.reload()
        self.dialog.table.selectRow(self.dialog.model.row_of(self.job.id))

    def test_the_user_is_warned_and_can_decline(self):
        opened = []
        self.dialog.open_submit_dialog = lambda **kwargs: opened.append(kwargs)
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as asked:
            self.dialog._resubmit_selected()
        asked.assert_called_once()
        self.assertIn("no longer exists", asked.call_args[0][2])
        self.assertEqual(opened, [], "declining still opened the wizard")

    def test_accepting_opens_the_wizard(self):
        opened = []
        self.dialog.open_submit_dialog = lambda **kwargs: opened.append(kwargs)
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.dialog._resubmit_selected()
        self.assertEqual(len(opened), 1)

    def test_a_job_whose_host_still_exists_is_not_questioned(self):
        job = self.service.submit(self.host, make_preset(), "mol", [self.make_input()])
        self.dialog.model.reload()
        self.dialog.table.selectRow(self.dialog.model.row_of(job.id))
        self.dialog.open_submit_dialog = lambda **kwargs: None
        with patch("job_manager.jobs_dialog.QMessageBox.question") as asked:
            self.dialog._resubmit_selected()
        asked.assert_not_called()


class TestExportAndClearButtons(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(Job(id="j1", name="alpha", state=STATE_RUNNING, submitted_at=1000.0))
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.close)

    def export_to(self, name, extension):
        target = os.path.join(self.tmp, name)
        with patch(
            "job_manager.jobs_dialog.QFileDialog.getSaveFileName", return_value=(target, "")
        ):
            self.dialog._export(extension)
        return target

    def test_the_three_buttons_exist(self):
        self.assertTrue(self.dialog.btn_save_as.isEnabled())
        self.assertTrue(self.dialog.btn_export_csv.isEnabled())
        self.assertTrue(self.dialog.btn_clear.isEnabled())

    def test_exporting_json(self):
        path = self.export_to("jobs.json", ".json")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["jobs"][0]["name"], "alpha")

    def test_exporting_csv(self):
        path = self.export_to("jobs.csv", ".csv")
        with open(path, encoding="utf-8") as handle:
            self.assertIn("alpha", handle.read())

    def test_a_missing_extension_is_added(self):
        target = os.path.join(self.tmp, "noext")
        with patch(
            "job_manager.jobs_dialog.QFileDialog.getSaveFileName", return_value=(target, "")
        ):
            self.dialog._export(".csv")
        self.assertTrue(os.path.exists(target + ".csv"))

    def test_cancelling_the_file_dialog_writes_nothing(self):
        with patch("job_manager.jobs_dialog.QFileDialog.getSaveFileName", return_value=("", "")):
            self.dialog._export(".json")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cancelled.json")))

    def test_exporting_an_empty_list_says_so_instead_of_writing(self):
        self.store.jobs = {}
        with patch("job_manager.jobs_dialog.QFileDialog.getSaveFileName") as chooser:
            self.dialog._export(".csv")
        chooser.assert_not_called()

    def test_an_unwritable_target_is_reported_not_raised(self):
        with patch(
            "job_manager.jobs_dialog.QFileDialog.getSaveFileName",
            return_value=(os.path.join(self.tmp, "sub", "x.csv"), ""),
        ):
            with patch("job_manager.store.JobStore.export_jobs", side_effect=OSError("nope")):
                with patch("job_manager.jobs_dialog.QMessageBox.warning") as warned:
                    self.dialog._export(".csv")
        warned.assert_called_once()

    def test_clearing_empties_the_table_and_archives(self):
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.dialog._clear_jobs()
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(len(self.store.archived_files()), 1)
        self.assertEqual(self.dialog.model.rowCount(), 0)

    def test_declining_keeps_everything(self):
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.dialog._clear_jobs()
        self.assertIn("j1", self.store.jobs)
        self.assertEqual(self.store.archived_files(), [])

    def test_the_confirmation_warns_about_still_active_jobs(self):
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as asked:
            self.dialog._clear_jobs()
        self.assertIn("still active", asked.call_args[0][2])

    def test_clearing_an_empty_list_asks_nothing(self):
        self.store.jobs = {}
        with patch("job_manager.jobs_dialog.QMessageBox.question") as asked:
            self.dialog._clear_jobs()
        asked.assert_not_called()


class TestOpeningAJobList(DialogTestCase):
    """Read-only if the file says archived; merged into the live table if not."""

    def setUp(self):
        super().setUp()
        self.store.add_job(Job(id="j1", name="alpha", state=STATE_RUNNING, submitted_at=1000.0))
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.close)

    def archived_file(self):
        path, _ = self.store.clear_jobs()
        return path

    def exported_file(self):
        return self.store.export_jobs(os.path.join(self.tmp, "exported.pmejbs"))

    def test_an_archived_list_opens_read_only(self):
        path = self.archived_file()
        self.assertTrue(self.dialog.open_job_list(path))
        self.assertTrue(self.dialog.viewing_archive())
        self.assertEqual(self.dialog.model.rowCount(), 1)
        self.assertEqual(self.store.jobs, {}, "an archived list must not be imported")

    def test_every_action_is_disabled_while_viewing_one(self):
        self.dialog.open_job_list(self.archived_file())
        self.dialog.table.selectRow(0)
        for button in (
            self.dialog.btn_cancel,
            self.dialog.btn_download,
            self.dialog.btn_open,
            self.dialog.btn_tail,
            self.dialog.btn_resubmit,
            self.dialog.btn_remove,
            self.dialog.btn_save_as,
            self.dialog.btn_export_csv,
            self.dialog.btn_clear,
        ):
            self.assertFalse(button.isEnabled(), button.text())

    def test_the_banner_points_at_the_folder_for_deleting(self):
        self.dialog.open_job_list(self.archived_file())
        self.assertFalse(self.dialog.lbl_archive.isHidden())
        text = self.dialog.lbl_archive.text()
        self.assertIn("read only", text)
        self.assertIn("delete", text.lower())
        self.assertIn(self.store.archive_dir(), text)

    def test_going_back_restores_the_live_list(self):
        self.dialog.open_job_list(self.archived_file())
        self.dialog._exit_archive()
        self.assertFalse(self.dialog.viewing_archive())
        self.assertTrue(self.dialog.lbl_archive.isHidden())
        self.assertTrue(self.dialog.btn_save_as.isEnabled())

    def test_an_unflagged_list_is_offered_for_import(self):
        path = self.exported_file()
        self.store.clear_jobs()
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.assertTrue(self.dialog.open_job_list(path))
        self.assertFalse(self.dialog.viewing_archive())
        self.assertIn("j1", self.store.jobs)

    def test_declining_the_import_changes_nothing(self):
        path = self.exported_file()
        self.store.clear_jobs()
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.assertFalse(self.dialog.open_job_list(path))
        self.assertEqual(self.store.jobs, {})

    def test_opening_a_working_list_leaves_the_archive_view(self):
        # Switching lists without leaving the read-only view left the table
        # showing the archive with every button disabled, while tracking and
        # saving had already moved to the file just opened.
        path = self.exported_file()
        self.dialog.open_job_list(self.archived_file())
        self.assertTrue(self.dialog.viewing_archive())
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.assertTrue(self.dialog.open_job_list(path))
        self.assertFalse(self.dialog.viewing_archive())
        self.assertTrue(self.dialog.lbl_archive.isHidden())
        self.assertTrue(self.dialog.btn_remove.isEnabled() or self.dialog.btn_clear.isEnabled())
        self.assertEqual(self.dialog.model.rowCount(), len(self.store.jobs))

    def test_going_back_to_the_default_list_leaves_the_archive_view_too(self):
        self.dialog.open_job_list(self.archived_file())
        self.dialog._use_default_job_list()
        self.assertFalse(self.dialog.viewing_archive())
        self.assertTrue(self.store.using_default_jobs_file())

    def test_an_empty_file_is_reported(self):
        path = os.path.join(self.tmp, "empty.pmejbs")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"jobs": []}')
        with patch("job_manager.jobs_dialog.QMessageBox.warning") as warned:
            self.assertFalse(self.dialog.open_job_list(path))
        warned.assert_called_once()


class TestDropOpensAJobList(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(Job(id="j1", name="alpha", state=STATE_RUNNING, submitted_at=1000.0))
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.close)

    def drop_event(self, *paths):
        from PyQt6.QtCore import QMimeData, QPointF, QUrl
        from PyQt6.QtCore import Qt as QtCore_Qt
        from PyQt6.QtGui import QDropEvent

        # The event does not take ownership of the mime data, so it has to
        # outlive the event or Qt reads freed memory.
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        self._mime_keepalive = getattr(self, "_mime_keepalive", [])
        self._mime_keepalive.append(mime)
        return QDropEvent(
            QPointF(10, 10),
            QtCore_Qt.DropAction.CopyAction,
            mime,
            QtCore_Qt.MouseButton.LeftButton,
            QtCore_Qt.KeyboardModifier.NoModifier,
        )

    def test_the_window_accepts_drops(self):
        self.assertTrue(self.dialog.acceptDrops())

    def test_dropping_an_archive_opens_it_read_only(self):
        path, _ = self.store.clear_jobs()
        self.dialog.dropEvent(self.drop_event(path))
        self.assertTrue(self.dialog.viewing_archive())

    def test_dropping_an_export_offers_to_import_it(self):
        path = self.store.export_jobs(os.path.join(self.tmp, "e.pmejbs"))
        self.store.clear_jobs()
        with patch(
            "job_manager.jobs_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.dialog.dropEvent(self.drop_event(path))
        self.assertIn("j1", self.store.jobs)

    def test_a_drag_of_the_right_file_is_accepted(self):
        path = self.store.export_jobs(os.path.join(self.tmp, "e.pmejbs"))
        dropped = self.dialog._dropped_job_list(self.drop_event(path))
        self.assertEqual(os.path.normcase(os.path.normpath(dropped)), os.path.normcase(path))

    def test_other_file_types_are_ignored(self):
        other = self.make_input("mol.inp")
        self.assertEqual(self.dialog._dropped_job_list(self.drop_event(other)), "")

    def test_dropping_several_files_is_ignored(self):
        one = self.store.export_jobs(os.path.join(self.tmp, "a.pmejbs"))
        two = self.store.export_jobs(os.path.join(self.tmp, "b.pmejbs"))
        self.assertEqual(self.dialog._dropped_job_list(self.drop_event(one, two)), "")

    def test_a_pre_extension_json_list_is_still_accepted(self):
        path = self.store.export_jobs(os.path.join(self.tmp, "old.json"))
        self.assertTrue(self.dialog._dropped_job_list(self.drop_event(path)))


class TestChainingAndScheduledStartInTheWizard(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(
            Job(
                id="prev",
                name="geom-opt",
                host_id=self.host.id,
                state=STATE_RUNNING,
                remote_job_id="98765",
                submitted_at=1000.0,
            )
        )
        self.dialog = SubmitDialog(self.service)
        self.addCleanup(self.dialog.close)
        self.dialog.prefill(files=[self.make_input()], name="freq", host_id=self.host.id)

    def preview(self):
        self.dialog._refresh_preview()
        return self.dialog.txt_preview.toPlainText()

    def test_the_predecessor_is_named(self):
        self.assertIn("geom-opt", self.dialog.lbl_chain.text())
        self.assertIn("98765", self.dialog.lbl_chain.text())

    def test_the_hint_names_the_mechanism(self):
        self.assertIn("depend", self.dialog.lbl_chain.text().lower())

    def test_the_dependency_reaches_the_preview(self):
        self.dialog.chk_chain.setChecked(True)
        self.assertIn("--dependency=afterok:98765", self.preview())

    def test_unticking_removes_it(self):
        self.dialog.chk_chain.setChecked(False)
        self.assertNotIn("dependency", self.preview())

    def test_nothing_to_chain_behind_disables_the_box(self):
        self.store.jobs["prev"].touch("DONE")
        self.dialog._update_chain_row()
        self.assertFalse(self.dialog.chk_chain.isEnabled())
        self.assertFalse(self.dialog.chain_requested())

    def test_the_preview_and_the_submission_agree(self):
        # They asked different questions once, so the preview could show a
        # dependency that submitting would not apply.
        self.store.jobs["prev"].touch("DONE")
        self.dialog._update_chain_row()
        self.dialog.chk_chain.setChecked(True)
        self.assertNotIn("dependency", self.preview())
        submitted = []
        self.service.submit = lambda *a, **k: submitted.append(k)
        self.dialog._submit()
        self.assertIsNone(submitted[0]["after_job"])

    def test_switching_to_the_preview_tab_does_not_drop_the_dependency(self):
        # A widget on a tab the user has switched away from is not "visible",
        # so reading isVisible() dropped the chaining for anyone who checked
        # the script before pressing Submit.
        from PyQt6.QtWidgets import QTabWidget

        self.dialog.chk_chain.setChecked(True)
        self.dialog.show()
        self.addCleanup(self.dialog.hide)
        tabs = self.dialog.findChild(QTabWidget)
        for index in range(tabs.count()):
            tabs.setCurrentIndex(index)
            self.assertTrue(
                self.dialog.chain_requested(), f"dependency lost on tab {tabs.tabText(index)!r}"
            )

    def test_a_genuinely_hidden_checkbox_still_means_no(self):
        self.dialog.chk_chain.setVisible(False)
        self.assertFalse(self.dialog.chain_requested())

    def test_a_scheduled_start_reaches_the_preview(self):
        from PyQt6.QtCore import QDateTime

        self.dialog.chk_start_at.setChecked(True)
        self.dialog.dt_start_at.setDateTime(QDateTime.currentDateTime().addSecs(7200))
        self.assertIn("--begin=", self.preview())

    def test_the_time_field_follows_the_checkbox(self):
        self.dialog.chk_start_at.setChecked(False)
        self.assertFalse(self.dialog.dt_start_at.isEnabled())
        self.dialog.chk_start_at.setChecked(True)
        self.assertTrue(self.dialog.dt_start_at.isEnabled())

    def test_unchecked_means_start_now(self):
        self.dialog.chk_start_at.setChecked(False)
        self.assertEqual(self.dialog.selected_start_time(), 0.0)
        self.assertNotIn("--begin", self.preview())

    def test_both_are_passed_to_submit(self):
        from PyQt6.QtCore import QDateTime

        self.dialog.chk_chain.setChecked(True)
        self.dialog.chk_start_at.setChecked(True)
        self.dialog.dt_start_at.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        submitted = []
        self.service.submit = lambda *a, **k: submitted.append(k)
        self.dialog._submit()
        self.assertEqual(submitted[0]["after_job"].id, "prev")
        self.assertGreater(submitted[0]["start_after"], 0)

    def test_the_job_record_keeps_both(self):
        job = self.service.submit(
            self.host,
            make_preset(),
            "later",
            [self.make_input()],
            after_job=self.store.jobs["prev"],
            start_after=1786000000.0,
        )
        self.assertEqual(job.after_job_id, "prev")
        self.assertEqual(job.start_after, 1786000000.0)
        self.assertEqual(JobStore(self.tmp).jobs[job.id].start_after, 1786000000.0)


class TestCommandTemplateDropdown(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = SubmitDialog(self.service)
        self.addCleanup(self.dialog.close)

    def labels(self):
        return [
            self.dialog.cmb_template.itemText(i) for i in range(self.dialog.cmb_template.count())
        ]

    def choose(self, label):
        self.dialog._on_template_chosen(self.labels().index(label))

    def test_the_built_in_programs_are_listed(self):
        for label in ("ORCA", "Gaussian 16", "VASP", "NWChem", "Psi4"):
            self.assertIn(label, self.labels())

    def test_choosing_one_fills_the_command_field(self):
        self.choose("Gaussian 16")
        self.assertEqual(self.dialog.txt_command.text(), "g16 {input}")

    def test_the_dropdown_returns_to_its_placeholder(self):
        self.choose("ORCA")
        self.assertEqual(self.dialog.cmb_template.currentIndex(), 0)

    def test_the_command_stays_editable(self):
        self.choose("ORCA")
        self.dialog.txt_command.setText("my own command [input]")
        self.assertEqual(self.dialog.collect_preset().command_template, "my own command [input]")

    def test_saved_templates_appear_and_can_be_chosen(self):
        self.store.add_user_template("My VASP", "srun vasp_std > [output]")
        self.dialog._reload_templates()
        self.assertIn("My VASP", self.labels())
        self.choose("My VASP")
        self.assertEqual(self.dialog.txt_command.text(), "srun vasp_std > [output]")

    def test_saving_the_current_command(self):
        self.dialog.txt_command.setText("orca [input] > [output]")
        with patch("job_manager.submit_dialog.QInputDialog.getText", return_value=("Mine", True)):
            self.dialog._save_user_template()
        self.assertEqual(
            JobStore(self.tmp).user_templates(),
            [{"label": "Mine", "command": "orca [input] > [output]"}],
        )

    def test_saving_an_empty_command_is_refused(self):
        self.dialog.txt_command.setText("   ")
        with patch("job_manager.submit_dialog.QMessageBox.information") as warned:
            self.dialog._save_user_template()
        warned.assert_called_once()
        self.assertEqual(self.store.user_templates(), [])

    def test_deleting_a_saved_template(self):
        self.store.add_user_template("Mine", "x")
        self.dialog._reload_templates()
        with patch("job_manager.submit_dialog.QInputDialog.getItem", return_value=("Mine", True)):
            self.dialog._delete_user_template()
        self.assertEqual(JobStore(self.tmp).user_templates(), [])

    def test_prefill_reorders_the_list_for_the_input(self):
        path = self.make_input("mol.nw")
        self.dialog.prefill(files=[path], name="p")
        self.assertEqual(self.labels()[1], "NWChem")

    def test_prefill_fills_an_empty_command_from_the_extension(self):
        self.dialog.txt_command.setText("")
        self.dialog.prefill(files=[self.make_input("mol.nw")], name="p")
        self.assertIn("nwchem", self.dialog.txt_command.text())

    def test_prefill_never_overwrites_a_command_the_user_already_has(self):
        self.dialog.txt_command.setText("keep me [input]")
        self.dialog.prefill(files=[self.make_input("mol.nw")], name="p")
        self.assertEqual(self.dialog.txt_command.text(), "keep me [input]")

    def test_an_ambiguous_extension_leaves_the_command_empty(self):
        # .inp belongs to ORCA, CP2K and GAMESS alike.
        self.dialog.txt_command.setText("")
        self.dialog.prefill(files=[self.make_input("mol.inp")], name="p")
        self.assertEqual(self.dialog.txt_command.text(), "")


if __name__ == "__main__":
    unittest.main()


class TestJobDetails(DialogTestCase):
    """The record, and the four settings that still decide what happens next."""

    def setUp(self):
        super().setUp()
        self.job = Job(
            id="j1",
            name="opt",
            host_id=self.host.id,
            host_name=self.host.name,
            scheduler="slurm",
            remote_job_id="42",
            state=STATE_RUNNING,
            remote_dir="~/work/opt",
            log_file="job.log",
            command="#!/bin/bash\norca mol.inp\n",
            fetch_globs=["*.out"],
            preset=make_preset(cpus_per_task=8, memory="24000M").to_dict(),
        )
        self.store.add_job(self.job)
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.table.selectRow(0)

    def details(self):
        from job_manager.details_dialog import JobDetailsDialog

        self.dialog._show_details()
        dialog = self.dialog._detail_dialogs[-1]
        self.assertIsInstance(dialog, JobDetailsDialog)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_the_record_names_the_resources_it_asked_for(self):
        text = self.dialog._describe(self.job)
        self.assertIn("CPUs per task", text)
        self.assertIn("8", text)
        self.assertIn("24000M", text)

    def test_the_record_shows_the_script_that_ran(self):
        self.assertIn("orca mol.inp", self.dialog._describe(self.job))

    def test_the_record_says_whether_the_login_files_are_read(self):
        # The one host setting that decides whether the command is found.
        self.assertIn("Reads login files", self.dialog._describe(self.job))

    def test_the_download_settings_are_editable_and_persist(self):
        dialog = self.details()
        dialog.txt_globs.setText("*.out, *.gbw")
        dialog.chk_auto.setChecked(False)
        dialog.txt_local.setText(self.tmp)

        dialog._save()

        stored = JobStore(self.tmp).jobs["j1"]
        self.assertEqual(stored.fetch_globs, ["*.out", "*.gbw"])
        self.assertFalse(stored.auto_download)
        self.assertEqual(stored.local_dir, self.tmp)

    def test_renaming_a_job_reaches_the_table(self):
        dialog = self.details()
        dialog.txt_name.setText("optimisation")

        dialog._save()

        self.assertEqual(JobStore(self.tmp).jobs["j1"].name, "optimisation")

    def test_a_blank_name_is_refused_rather_than_stored(self):
        dialog = self.details()
        dialog.txt_name.setText("   ")
        dialog._save()
        self.assertEqual(self.job.name, "opt")

    def test_closing_it_twice_does_not_raise(self):
        # QDialogButtonBox emits rejected for a Close button, so wiring its
        # clicked as well called reject() twice and finished fired twice. The
        # second cleanup then raised ValueError out of a Qt slot, which the
        # host reports to the user as a plugin crash.
        dialog = self.details()

        dialog.reject()
        dialog.finished.emit(0)

        self.assertNotIn(dialog, self.dialog._detail_dialogs)

    def test_details_works_for_an_archived_job(self):
        # It reads only what is recorded, so it needs no host and no network.
        self.dialog._archive_path = "/somewhere/jobs_2026.pmejbs"
        self.dialog._update_buttons()
        self.assertTrue(self.dialog.btn_details.isEnabled())
        self.assertFalse(self.dialog.btn_tail.isEnabled())


class TestTailWindow(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(
            Job(
                id="j1",
                name="opt",
                host_id=self.host.id,
                remote_dir="~/work/opt",
                log_file="job.log",
            )
        )
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.table.selectRow(0)

    def test_tailing_opens_a_window_of_its_own(self):
        self.dialog._tail_selected()
        self.assertIsNotNone(self.dialog._tail_dialog)
        self.addCleanup(self.dialog._tail_dialog.deleteLater)
        self.assertIn("job.log", self.dialog._tail_dialog.windowTitle())

    def test_the_tail_lands_in_that_window_not_the_strip(self):
        self.dialog._tail_selected()
        self.addCleanup(self.dialog._tail_dialog.deleteLater)

        self.dialog._show_log("SCF converged\n")

        self.assertIn("SCF converged", self.dialog._tail_dialog.view.toPlainText())
        self.assertNotIn("SCF converged", self.dialog.txt_log.toPlainText())

    def test_a_second_tail_reuses_the_window(self):
        self.dialog._tail_selected()
        first = self.dialog._tail_dialog
        self.addCleanup(first.deleteLater)
        self.dialog._tail_selected()
        self.assertIs(self.dialog._tail_dialog, first)

    def test_double_clicking_a_row_tails_it(self):
        self.dialog.table.doubleClicked.emit(self.dialog.model.index(0, 0))
        self.assertIsNotNone(self.dialog._tail_dialog)
        self.addCleanup(self.dialog._tail_dialog.deleteLater)

    def test_a_tail_with_no_window_open_still_goes_somewhere(self):
        # The window can be closed while the read is in flight.
        self.dialog._show_log("orphaned output")
        self.assertIn("orphaned output", self.dialog.txt_log.toPlainText())


class TestSwitchingHostsWithEdits(DialogTestCase):
    """Saving must answer the question once, not ask it again."""

    def setUp(self):
        super().setUp()
        self.store.add_host(make_host(id="second", name="workstation"))
        self.dialog = HostsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.show()
        self.addCleanup(self.dialog.hide)

    def select(self, host_id):
        from PyQt6.QtCore import Qt

        for row in range(self.dialog.list.count()):
            if self.dialog.list.item(row).data(Qt.ItemDataRole.UserRole) == host_id:
                self.dialog.list.setCurrentRow(row)
                return
        self.fail(host_id)

    def test_saving_asks_once(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QMessageBox

        self.dialog.txt_hostname.setText("edited.example.org")

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Save
        ) as asked:
            self.select("second")

        asked.assert_called_once()
        self.assertEqual(JobStore(self.tmp).hosts[self.host.id].hostname, "edited.example.org")

    def test_and_the_host_you_clicked_is_the_one_you_get(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QMessageBox

        self.dialog.txt_hostname.setText("edited.example.org")

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save):
            self.select("second")

        self.assertEqual(self.dialog._current.id, "second")

    def test_discarding_asks_once_too(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QMessageBox

        self.dialog.txt_hostname.setText("thrown.away")

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard
        ) as asked:
            self.select("second")

        asked.assert_called_once()
        self.assertNotEqual(JobStore(self.tmp).hosts[self.host.id].hostname, "thrown.away")

    def test_saving_twice_over_asks_nothing_the_second_time(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QMessageBox

        self.dialog.txt_hostname.setText("edited.example.org")
        self.dialog._save_current()

        with patch.object(QMessageBox, "question") as asked:
            self.select("second")

        asked.assert_not_called()


class TestTheSaveButtonIsQuiet(DialogTestCase):
    """Pressing Save must not then ask whether to save."""

    def setUp(self):
        super().setUp()
        self.dialog = HostsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.show()
        self.addCleanup(self.dialog.hide)

    def test_pressing_it_asks_nothing(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QMessageBox

        self.dialog.txt_hostname.setText("edited.example.org")

        with patch.object(QMessageBox, "question") as asked:
            self.dialog.btn_save.click()

        asked.assert_not_called()
        self.assertEqual(JobStore(self.tmp).hosts[self.host.id].hostname, "edited.example.org")

    def test_the_list_still_refreshes_after_the_button(self):
        # clicked() carries a bool, which would land in the reload argument.
        self.dialog.txt_name.setText("renamed")

        self.dialog.btn_save.click()

        labels = [self.dialog.list.item(row).text() for row in range(self.dialog.list.count())]
        self.assertTrue(any("renamed" in label for label in labels), labels)

    def test_closing_straight_after_a_save_asks_nothing(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QMessageBox

        self.dialog.txt_hostname.setText("edited.example.org")
        self.dialog.btn_save.click()

        with patch.object(QMessageBox, "question") as asked:
            self.dialog.close()

        asked.assert_not_called()


class TestTheHostsDialogOpensBigEnough(DialogTestCase):
    def test_it_asks_for_room_but_never_more_than_the_screen(self):
        # 900 where there is space for it; less on a small screen, where the
        # editing column scrolls anyway.
        from PyQt6.QtWidgets import QApplication

        dialog = HostsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        room = screen.availableGeometry().width()
        self.assertEqual(dialog.width(), min(900, room - 80))
        self.assertLessEqual(dialog.width(), room)

    def test_it_never_opens_taller_than_the_screen(self):
        from PyQt6.QtWidgets import QApplication

        dialog = HostsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.assertLessEqual(dialog.height(), screen.availableGeometry().height())
