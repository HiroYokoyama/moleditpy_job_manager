"""File entry, ordering and upload safety against real Qt widgets."""

import os
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QItemSelectionModel, QUrl  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402


class TestInputFileTable(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = SubmitDialog(self.service)
        self.addCleanup(self.dialog.close)

    def test_paste_quotes_spaces_and_file_url_deduplicates(self):
        first = self.make_input("input with spaces.inp")
        second = self.make_input("second.nw")
        self.dialog.txt_add_paths.setPlainText(
            f'"{first}"\n{QUrl.fromLocalFile(second).toString()}\n{first}'
        )
        self.dialog._add_pasted_paths()
        self.assertEqual(self.dialog.selected_files(), [first, second])
        self.assertEqual(self.dialog.file_table.item(0, 0).text(), "input with spaces.inp")
        self.assertEqual(self.dialog.txt_selected_path.text(), first)
        self.assertEqual(self.dialog.txt_add_paths.toPlainText(), "")

    def test_invalid_paste_does_not_partially_add(self):
        first = self.make_input()
        self.dialog.txt_add_paths.setPlainText(f"{first}\n{self.tmp}/missing.inp")
        self.dialog._add_pasted_paths()
        self.assertEqual(self.dialog.selected_files(), [])
        self.assertIn("No paths were added", self.dialog.lbl_file_message.text())

    def test_move_changes_primary_preview_and_template_choices(self):
        first = self.make_input("first.inp")
        second = self.make_input("second.nw")
        self.dialog.add_files([first, second])
        self.dialog.file_table.selectRow(1)
        self.dialog._move_files(-1)
        self.assertEqual(self.dialog.selected_files(), [second, first])
        self.assertEqual(self.dialog.cmb_template.itemText(1), "NWChem")
        self.assertIn("second.nw", self.dialog.txt_preview.toPlainText())
        self.assertEqual(self.dialog.txt_selected_path.text(), second)

    def test_remove_first_refreshes_template_choices(self):
        self.dialog.add_files([self.make_input("first.inp"), self.make_input("second.nw")])
        self.dialog.file_table.selectRow(0)
        self.dialog._remove_file()
        self.assertEqual(self.dialog.cmb_template.itemText(1), "NWChem")

    def test_multiple_selected_rows_move_as_a_block_then_remove(self):
        paths = [self.make_input(f"{i}.inp") for i in range(4)]
        self.dialog.add_files(paths)
        table = self.dialog.file_table
        table.clearSelection()
        for row in (1, 2):
            table.selectionModel().select(
                table.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        self.dialog._move_files(-1)
        self.assertEqual(self.dialog.selected_files(), [paths[1], paths[2], paths[0], paths[3]])
        self.dialog._remove_file()
        self.assertEqual(self.dialog.selected_files(), [paths[0], paths[3]])

    def test_same_upload_filename_is_rejected_before_submission(self):
        first = self.make_input()
        other_dir = os.path.join(self.tmp, "other")
        os.mkdir(other_dir)
        second = os.path.join(other_dir, os.path.basename(first))
        with open(second, "w") as stream:
            stream.write("x")
        self.dialog.add_files([first, second])
        with patch("job_manager.submit_dialog.QMessageBox.warning") as warning:
            self.dialog._submit()
        self.assertIn("overwrite", warning.call_args.args[2])
        self.assertEqual(self.store.jobs, {})

    def test_same_filename_is_allowed_for_separate_jobs(self):
        first = self.make_input()
        other_dir = os.path.join(self.tmp, "other")
        os.mkdir(other_dir)
        second = os.path.join(other_dir, os.path.basename(first))
        with open(second, "w") as stream:
            stream.write("x")
        self.dialog.add_files([first, second], batch=True)
        with patch.object(self.dialog, "_submit_batch") as submit:
            self.dialog._submit()
        submit.assert_called_once()

    def test_boundary_move_does_not_change_order(self):
        path = self.make_input()
        self.dialog.add_files([path])
        self.dialog._move_files(-1)
        self.dialog._move_files(1)
        self.assertEqual(self.dialog.selected_files(), [path])

    def test_details_lists_and_copies_recorded_paths(self):
        from job_manager.details_dialog import JobDetailsDialog
        from job_manager.models import Job

        path = self.make_input()
        job = Job(name="Example", input_files=[path], remote_dir="/opt/runs/example")
        details = JobDetailsDialog(self.service, job, "record", "Details")
        self.addCleanup(details.close)
        details.file_table.selectRow(0)
        self.assertEqual(details.txt_file_path.text(), path)
        self.assertEqual(details.file_table.item(0, 3).text(), "Input")
        details.txt_local.setText(self.tmp)
        details._save()
        self.assertEqual(details.results_folder_view.text(), self.tmp)
