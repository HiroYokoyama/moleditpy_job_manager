"""The file text viewer: read only by default, File > Edit Mode to change a file."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from job_manager.output_file_dialog import OutputFileSelectorDialog  # noqa: E402
from job_manager.result_view import _text_windows, show_text_window  # noqa: E402
from job_manager.text_dialog import TextDialog  # noqa: E402
from job_manager.text_editor import TextEditorDialog  # noqa: E402

from .fakes import make_job  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402

BUTTONS = QMessageBox.StandardButton


class EditorCase(DialogTestCase):
    def editor(self, data: bytes = b"one\r\ntwo\r\n", name: str = "a.inp") -> TextEditorDialog:
        self.path = os.path.join(self.tmp, name)
        with open(self.path, "wb") as handle:
            handle.write(data)
        return self.open(self.path)

    def open(self, path) -> TextEditorDialog:
        dialog = TextEditorDialog(path)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def edit(self, dialog: TextEditorDialog, text: str) -> None:
        dialog.view.moveCursor(dialog.view.textCursor().MoveOperation.End)
        dialog.view.insertPlainText(text)

    def disk(self) -> bytes:
        with open(self.path, "rb") as handle:
            return handle.read()


class TestViewing(EditorCase):
    def test_a_file_opens_read_only_and_ctrl_s_is_save_as(self):
        dialog = self.editor()
        self.assertTrue(dialog.view.isReadOnly())
        self.assertEqual(dialog.act_save_as.shortcut(), QKeySequence("Ctrl+S"))
        self.assertFalse(dialog.act_save.isEnabled())
        self.assertFalse(dialog.btn_save.isVisibleTo(dialog))

    def test_save_as_of_an_unedited_file_is_a_byte_for_byte_copy(self):
        dialog = self.editor(b"a\r\nb\nc\r")
        target = os.path.join(self.tmp, "copy.inp")
        with patch(
            "job_manager.text_dialog.QFileDialog.getSaveFileName", return_value=(target, "")
        ):
            self.assertTrue(dialog.save_as())
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"a\r\nb\nc\r")
        self.assertEqual(dialog.path, target)

    def test_save_as_of_a_large_file_copies_all_of_it(self):
        data = b"line\n" * 2_000_000
        dialog = self.editor(data)
        self.assertFalse(dialog.act_edit_mode.isEnabled())
        target = os.path.join(self.tmp, "copy.out")
        with patch(
            "job_manager.text_dialog.QFileDialog.getSaveFileName", return_value=(target, "")
        ):
            self.assertTrue(dialog.save_as())
        self.assertEqual(os.path.getsize(target), len(data))

    def test_cancelling_save_as_writes_nothing(self):
        dialog = self.editor()
        with patch("job_manager.text_dialog.QFileDialog.getSaveFileName", return_value=("", "")):
            self.assertFalse(dialog.save_as())
        self.assertEqual(os.listdir(self.tmp).count("a.inp"), 1)

    def test_the_format_is_shown(self):
        dialog = self.editor("水\r\n".encode("cp932"))
        self.assertEqual(dialog.lbl_format.text(), "Shift_JIS (CP932) | CRLF")

    def test_a_plain_tail_window_can_save_what_it_shows(self):
        dialog = TextDialog("t", "alpha\nbeta\n", save_name="job.log")
        self.addCleanup(dialog.deleteLater)
        target = os.path.join(self.tmp, "kept.log")
        with patch(
            "job_manager.text_dialog.QFileDialog.getSaveFileName", return_value=(target, "")
        ) as asked:
            self.assertTrue(dialog.save_as())
        self.assertTrue(asked.call_args.args[2].endswith("job.log"))
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha\nbeta\n")


class TestEditMode(EditorCase):
    def test_edit_mode_makes_ctrl_s_overwrite(self):
        dialog = self.editor()
        dialog.act_edit_mode.setChecked(True)
        self.assertFalse(dialog.view.isReadOnly())
        self.assertEqual(dialog.act_save.shortcut(), QKeySequence("Ctrl+S"))
        self.assertEqual(dialog.act_save_as.shortcut(), QKeySequence("Ctrl+Shift+S"))
        dialog.act_edit_mode.setChecked(False)
        self.assertTrue(dialog.view.isReadOnly())
        self.assertEqual(dialog.act_save_as.shortcut(), QKeySequence("Ctrl+S"))

    def test_saving_keeps_encoding_and_line_endings(self):
        dialog = self.editor("水\r\n".encode("cp932"))
        dialog.act_edit_mode.setChecked(True)
        self.edit(dialog, "酸\n")
        self.assertTrue(dialog.is_modified())

        self.assertTrue(dialog.save())

        self.assertEqual(self.disk(), "水\r\n酸\r\n".encode("cp932"))
        self.assertFalse(dialog.is_modified())

    def test_a_non_breaking_space_survives_saving(self):
        dialog = self.editor("a b\n".encode("utf-8"))
        dialog.act_edit_mode.setChecked(True)
        self.edit(dialog, "c\n")
        dialog.save()
        self.assertEqual(self.disk(), "a b\nc\n".encode("utf-8"))

    def test_choosing_an_encoding_and_line_ending(self):
        dialog = self.editor(b"one\r\n")
        dialog.act_edit_mode.setChecked(True)
        utf16 = next(a for a in dialog._encoding_actions.actions() if a.text() == "UTF-16 LE")
        lf = next(a for a in dialog._newline_actions.actions() if a.text() == "LF")

        utf16.trigger()
        lf.trigger()

        self.assertTrue(dialog.is_modified())
        self.assertEqual(dialog.lbl_format.text(), "UTF-16 LE | LF")
        dialog.save()
        self.assertEqual(self.disk(), b"\xff\xfe" + "one\n".encode("utf-16-le"))
        self.assertFalse(dialog.is_modified())

    def test_format_menus_are_only_for_edit_mode(self):
        dialog = self.editor()
        self.assertFalse(dialog.menu_save_encoding.isEnabled())
        self.assertFalse(dialog.menu_save_newline.isEnabled())
        dialog.act_edit_mode.setChecked(True)
        self.assertTrue(dialog.menu_save_encoding.isEnabled())
        self.assertTrue(dialog.menu_save_newline.isEnabled())

    def test_a_character_the_encoding_lacks_is_reported_and_nothing_written(self):
        dialog = self.editor("水\n".encode("cp932"))
        dialog.act_edit_mode.setChecked(True)
        self.edit(dialog, "\U0001f600")

        self.assertFalse(dialog.save())

        self.assertIn("Not saved", dialog.lbl_open_status.text())
        self.assertEqual(self.disk(), "水\n".encode("cp932"))
        self.assertTrue(dialog.is_modified())

    def test_reopening_with_an_encoding(self):
        dialog = self.editor("水".encode("euc_jp"))
        action = next(a for a in dialog.menu_reopen.actions() if a.text() == "EUC-JP")
        action.trigger()
        self.assertEqual(dialog.view.toPlainText(), "水")
        self.assertIn("EUC-JP", dialog.lbl_format.text())

    def test_reopening_with_a_wrong_encoding_changes_nothing(self):
        dialog = self.editor(b"\xff\xfe\xff")
        before = dialog.view.toPlainText()
        action = next(a for a in dialog.menu_reopen.actions() if a.text() == "UTF-8")
        action.trigger()
        self.assertEqual(dialog.view.toPlainText(), before)
        self.assertIn("not valid", dialog.lbl_open_status.text())

    def test_a_file_changed_on_disk_asks_before_overwriting(self):
        dialog = self.editor(b"one\n")
        dialog.act_edit_mode.setChecked(True)
        self.edit(dialog, "two\n")
        with open(self.path, "wb") as handle:
            handle.write(b"written by something else\n")
        with patch.object(QMessageBox, "question", return_value=BUTTONS.No) as asked:
            self.assertFalse(dialog.save())
        asked.assert_called_once()
        self.assertEqual(self.disk(), b"written by something else\n")


class TestUnsavedChanges(EditorCase):
    def modified(self) -> TextEditorDialog:
        dialog = self.editor(b"one\n")
        dialog.show()
        dialog.act_edit_mode.setChecked(True)
        self.edit(dialog, "two\n")
        return dialog

    def test_cancel_keeps_the_window_open(self):
        dialog = self.modified()
        with patch.object(QMessageBox, "question", return_value=BUTTONS.Cancel):
            dialog.reject()
            self.assertTrue(dialog.isVisible())
            dialog.close()
            self.assertTrue(dialog.isVisible())
        dialog.view.document().setModified(False)
        dialog.close()

    def test_discard_closes_without_writing(self):
        dialog = self.modified()
        with patch.object(QMessageBox, "question", return_value=BUTTONS.Discard):
            dialog.close()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.disk(), b"one\n")

    def test_save_writes_then_closes(self):
        dialog = self.modified()
        with patch.object(QMessageBox, "question", return_value=BUTTONS.Save):
            dialog.reject()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.disk(), b"one\ntwo\n")

    def test_leaving_edit_mode_can_be_cancelled(self):
        dialog = self.modified()
        with patch.object(QMessageBox, "question", return_value=BUTTONS.Cancel):
            dialog.act_edit_mode.setChecked(False)
        self.assertTrue(dialog.act_edit_mode.isChecked())
        self.assertFalse(dialog.view.isReadOnly())
        dialog.view.document().setModified(False)
        dialog.close()

    def test_leaving_edit_mode_with_discard_shows_the_file_again(self):
        dialog = self.modified()
        with patch.object(QMessageBox, "question", return_value=BUTTONS.Discard):
            dialog.act_edit_mode.setChecked(False)
        self.assertEqual(dialog.view.toPlainText(), "one\n")
        self.assertTrue(dialog.view.isReadOnly())
        dialog.close()

    def test_reload_asks_first(self):
        dialog = self.modified()
        with patch.object(QMessageBox, "question", return_value=BUTTONS.Cancel):
            dialog.act_reload.trigger()
        self.assertIn("two", dialog.view.toPlainText())
        dialog.view.document().setModified(False)
        dialog.close()


class TestNewFile(EditorCase):
    def test_new_starts_empty_in_edit_mode_and_ctrl_s_names_it(self):
        dialog = self.open(None)
        self.assertFalse(dialog.view.isReadOnly())
        self.assertFalse(dialog.act_reload.isEnabled())
        self.assertIn("Untitled", dialog.windowTitle())
        self.assertFalse(dialog.btn_open_in_moleditpy)
        self.edit(dialog, "text\n")
        target = os.path.join(self.tmp, "new.inp")
        with patch(
            "job_manager.text_dialog.QFileDialog.getSaveFileName", return_value=(target, "")
        ):
            self.assertTrue(dialog.save())
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"text\n")
        self.assertEqual(dialog.path, target)
        self.assertTrue(dialog.act_reload.isEnabled())
        self.assertIn("new.inp", dialog.windowTitle())

    def test_an_untouched_new_file_closes_without_asking(self):
        dialog = self.open(None)
        dialog.show()
        with patch.object(QMessageBox, "question") as asked:
            dialog.close()
        asked.assert_not_called()

    def test_file_new_opens_a_window_that_stays_alive(self):
        dialog = TextDialog("t", "x")
        self.addCleanup(dialog.deleteLater)
        before = list(_text_windows)
        dialog._new_file()
        created = [window for window in _text_windows if window not in before]
        self.assertEqual(len(created), 1)
        self.assertIsInstance(created[0], TextEditorDialog)
        self.assertTrue(created[0].isVisible())
        created[0].close()
        self.assertNotIn(created[0], _text_windows)

    def test_save_as_points_open_in_moleditpy_at_the_new_file(self):
        path = os.path.join(self.tmp, "a.inp")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x\n")
        dialog = show_text_window(path)
        self.assertTrue(dialog.btn_open_in_moleditpy.isEnabled())
        target = os.path.join(self.tmp, "b.txt")
        with patch(
            "job_manager.text_dialog.QFileDialog.getSaveFileName", return_value=(target, "")
        ):
            dialog.save_as()
        self.assertFalse(dialog.btn_open_in_moleditpy.isEnabled())
        dialog.close()


class TestTheBrowserButtons(DialogTestCase):
    def test_the_text_viewer_button_comes_first(self):
        dialog = OutputFileSelectorDialog(self.service, make_job())
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.addCleanup(dialog.hide)
        QApplication.instance().processEvents()
        self.assertEqual(dialog.btn_text.text(), "Open in Text Viewer")
        self.assertEqual(dialog.btn_open.text(), "Open in MoleditPy")
        self.assertLess(dialog.btn_text.x(), dialog.btn_open.x())
