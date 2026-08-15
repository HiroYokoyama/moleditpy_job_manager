"""The command-template editor: default-per-extension and named templates.

Both kinds could previously only be added from the submit wizard's dropdown;
a named template had a delete path (one at a time, through a QInputDialog
picker) but a per-extension default had none in the UI at all. These tests
cover add/edit/delete through the dialog directly against a real JobStore.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

from job_manager.store import JobStore  # noqa: E402
from job_manager.template_editor_dialog import TemplateEditorDialog  # noqa: E402


class TemplateEditorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="template_editor_")
        self.store = JobStore(self.tmp)
        self.dialog = TemplateEditorDialog(self.store)
        self.addCleanup(self.dialog.deleteLater)


class TestDefaultCommands(TemplateEditorTestCase):
    def test_empty_store_shows_a_placeholder(self):
        self.assertEqual(self.dialog.list_defaults.count(), 1)
        self.assertIn("No per-extension", self.dialog.list_defaults.item(0).text())

    def test_a_saved_default_is_listed(self):
        self.store.set_default_command(".inp", "orca {input} > {stem}.out")
        self.dialog._reload()
        self.assertEqual(self.dialog.list_defaults.count(), 1)
        self.assertIn(".inp", self.dialog.list_defaults.item(0).text())
        self.assertIn("orca", self.dialog.list_defaults.item(0).text())

    def test_editing_a_default_persists_the_new_command(self):
        self.store.set_default_command(".inp", "orca {input} > {stem}.out")
        self.dialog._reload()
        item = self.dialog.list_defaults.item(0)
        with patch(
            "job_manager.template_editor_dialog.QInputDialog.getText",
            return_value=("g16 {input}", True),
        ):
            self.dialog._edit_default(item)
        self.assertEqual(self.store.default_command_for(".inp")["command"], "g16 {input}")

    def test_cancelling_the_edit_leaves_the_default_alone(self):
        self.store.set_default_command(".inp", "orca {input} > {stem}.out")
        self.dialog._reload()
        item = self.dialog.list_defaults.item(0)
        with patch(
            "job_manager.template_editor_dialog.QInputDialog.getText",
            return_value=("g16 {input}", False),
        ):
            self.dialog._edit_default(item)
        self.assertEqual(
            self.store.default_command_for(".inp")["command"], "orca {input} > {stem}.out"
        )

    def test_deleting_a_default_forgets_it(self):
        self.store.set_default_command(".inp", "orca {input} > {stem}.out")
        self.dialog._reload()
        self.dialog.list_defaults.setCurrentRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._delete_selected_default()
        self.assertEqual(self.store.default_command_for(".inp"), {})
        self.assertIn("No per-extension", self.dialog.list_defaults.item(0).text())

    def test_declining_the_delete_keeps_it(self):
        self.store.set_default_command(".inp", "orca {input} > {stem}.out")
        self.dialog._reload()
        self.dialog.list_defaults.setCurrentRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.dialog._delete_selected_default()
        self.assertNotEqual(self.store.default_command_for(".inp"), {})


class TestNamedTemplates(TemplateEditorTestCase):
    def test_empty_store_shows_a_placeholder(self):
        self.assertEqual(self.dialog.list_templates.count(), 1)
        self.assertIn("No saved templates", self.dialog.list_templates.item(0).text())

    def test_a_saved_template_is_listed(self):
        self.store.add_user_template("my orca", "orca {input} > {stem}.out")
        self.dialog._reload()
        self.assertIn("my orca", self.dialog.list_templates.item(0).text())

    def test_editing_a_template_persists_the_new_command(self):
        self.store.add_user_template("my orca", "orca {input} > {stem}.out")
        self.dialog._reload()
        item = self.dialog.list_templates.item(0)
        with patch(
            "job_manager.template_editor_dialog.QInputDialog.getText",
            return_value=("orca {input} --new-flag > {stem}.out", True),
        ):
            self.dialog._edit_template(item)
        templates = {t["label"]: t["command"] for t in self.store.user_templates()}
        self.assertEqual(templates["my orca"], "orca {input} --new-flag > {stem}.out")

    def test_deleting_a_template_removes_it(self):
        self.store.add_user_template("my orca", "orca {input} > {stem}.out")
        self.dialog._reload()
        self.dialog.list_templates.setCurrentRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._delete_selected_template()
        self.assertEqual(self.store.user_templates(), [])


class TestOpenFromTheSubmitWizard(unittest.TestCase):
    """The wizard's 'Manage templates...' entry reaches this dialog."""

    def test_manage_templates_opens_the_editor(self):
        from .test_dialogs import DialogTestCase

        case = DialogTestCase(methodName="setUp")
        case.setUp()
        self.addCleanup(case.service.shutdown)

        from job_manager.submit_dialog import SubmitDialog

        dialog = SubmitDialog(case.service)
        self.addCleanup(dialog.deleteLater)
        with patch.object(TemplateEditorDialog, "exec", return_value=0) as exec_mock:
            dialog._manage_templates()
        exec_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
