"""Text results open in the plugin's own viewer, and the viewer can search.

A .txt result used to be handed to MoleditPy like any other. With the OpenBabel
plugin installed that meant OpenBabel's "txt" format -- one empty molecule per
line -- read on the GUI thread, which froze the application on any output of
real length, and only after the user's document had been cleared for it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager import jobs_dialog  # noqa: E402
from job_manager.jobs_dialog import (  # noqa: E402
    JobsDialog,
    is_text_file,
    read_text_for_view,
)
from job_manager.output_file_dialog import OutputFileSelectorDialog  # noqa: E402
from job_manager.text_dialog import TextDialog  # noqa: E402
from job_manager.tree_utils import IS_REMOTE_ROLE  # noqa: E402

from .fakes import make_job  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TextFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="textview_")

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class TestWhatCountsAsText(TextFileCase):
    def test_txt_in_any_case_is_text(self):
        self.assertTrue(is_text_file("/x/result.txt"))
        self.assertTrue(is_text_file("C:\\x\\RESULT.TXT"))

    def test_a_structure_or_an_analyser_output_is_not(self):
        for name in ("mol.xyz", "mol.out", "mol.log", "mol.fchk", "notes"):
            with self.subTest(name=name):
                self.assertFalse(is_text_file(name))

    def test_a_small_file_is_read_whole(self):
        path = self.write("a.txt", "one\ntwo\n")
        self.assertEqual(read_text_for_view(path), "one\ntwo\n")

    def test_a_large_file_keeps_its_end_and_says_so(self):
        path = self.write("big.txt", "".join(f"line {i}\n" for i in range(1000)))
        text = read_text_for_view(path, limit=200)
        self.assertTrue(text.startswith("[Showing the last"))
        self.assertTrue(text.rstrip().endswith("line 999"))
        # Cut at a line boundary: no half line after the notice.
        body = text.split("\n\n", 1)[1]
        self.assertTrue(body.startswith("line "))

    def test_bytes_that_are_not_utf8_do_not_stop_it(self):
        path = os.path.join(self.tmp, "bin.txt")
        with open(path, "wb") as handle:
            handle.write(b"ok \xff\xfe end\n")
        self.assertIn("ok", read_text_for_view(path))


class TestOpeningAResult(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.tmp = tempfile.mkdtemp(prefix="textresult_")

    def test_a_txt_result_never_reaches_moleditpy(self):
        path = os.path.join(self.tmp, "summary.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("energy -76.4\n")
        with patch.object(jobs_dialog, "open_in_host") as host:
            self.dialog.open_result_files([path])
        host.assert_not_called()
        viewers = [d for d in self.dialog._detail_dialogs if isinstance(d, TextDialog)]
        self.assertEqual(len(viewers), 1)
        self.assertIn("energy -76.4", viewers[0].view.toPlainText())
        for viewer in viewers:
            viewer.close()

    def test_the_viewer_holds_a_copy_not_the_file(self):
        # Read once into memory and closed: an open handle would lock the file
        # on Windows, and the next download of the same result could not
        # replace it while the window stayed open.
        path = os.path.join(self.tmp, "summary.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("first\n")
        self.dialog.show_text_file(path)
        viewer = self.dialog._detail_dialogs[-1]
        self.addCleanup(viewer.close)

        os.replace(path, path + ".old")
        os.remove(path + ".old")

        self.assertIn("first", viewer.view.toPlainText())

    def test_any_other_result_still_goes_to_moleditpy(self):
        path = os.path.join(self.tmp, "mol.xyz")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1\n\nH 0 0 0\n")
        with patch.object(jobs_dialog, "open_in_host", return_value=True) as host:
            self.dialog.open_result_files([path])
        host.assert_called_once_with(path)


class TestTheTextViewerButton(DialogTestCase):
    def test_any_file_can_be_read_as_text(self):
        tmp = tempfile.mkdtemp(prefix="textbutton_")
        path = os.path.join(tmp, "mol.out")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("FINAL SINGLE POINT ENERGY\n")
        job = make_job(local_dir=tmp, downloaded_files=[path], remote_dir="")
        seen = []
        opened = []
        dialog = OutputFileSelectorDialog(
            self.service, job, on_open_callback=opened.append, on_text_callback=seen.append
        )
        self.addCleanup(dialog.deleteLater)
        dialog.tree.setCurrentItem(None)
        self.assertFalse(dialog.btn_text.isEnabled())
        item = next(i for i in dialog._all_items if i.data(0, IS_REMOTE_ROLE) is not None)
        dialog.tree.setCurrentItem(item)
        self.assertTrue(dialog.btn_text.isEnabled())

        dialog.btn_text.click()

        self.assertEqual([os.path.normpath(p) for p in seen], [os.path.normpath(path)])
        self.assertEqual(opened, [])


class TestFind(unittest.TestCase):
    def setUp(self):
        self.viewer = TextDialog("t", "alpha\nbeta\nAlpha again\nbeta\n")
        self.addCleanup(self.viewer.deleteLater)

    def selected(self):
        return self.viewer.view.textCursor().selectedText()

    def line_of_match(self):
        return self.viewer.view.textCursor().blockNumber()

    def test_the_bar_is_hidden_until_asked_for(self):
        self.assertTrue(self.viewer.find_bar.isHidden())
        self.viewer.show_find()
        self.assertFalse(self.viewer.find_bar.isHidden())

    def test_next_moves_through_matches_and_wraps(self):
        self.viewer.txt_find.setText("beta")
        self.assertTrue(self.viewer.find())
        self.assertEqual(self.line_of_match(), 1)
        self.assertTrue(self.viewer.find())
        self.assertEqual(self.line_of_match(), 3)
        self.assertTrue(self.viewer.find())
        self.assertEqual(self.line_of_match(), 1)
        self.assertEqual(self.viewer.lbl_find.text(), "1 of 2, wrapped round")

    def test_previous_goes_back(self):
        self.viewer.txt_find.setText("beta")
        self.viewer.find()
        self.viewer.find()
        self.viewer.find(backward=True)
        self.assertEqual(self.line_of_match(), 1)

    def test_case_is_ignored_unless_asked(self):
        self.viewer.txt_find.setText("alpha")
        self.viewer.find()
        self.viewer.find()
        self.assertEqual(self.selected(), "Alpha")
        self.viewer.chk_case.setChecked(True)
        self.viewer.find()
        self.assertEqual(self.selected(), "alpha")
        self.viewer.find()
        self.assertEqual(self.line_of_match(), 0)

    def test_no_match_says_so(self):
        self.viewer.txt_find.setText("gamma")
        self.assertFalse(self.viewer.find())
        self.assertEqual(self.viewer.lbl_find.text(), "Not found")

    def test_escape_in_the_field_closes_the_bar_not_the_window(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        self.viewer.show()
        self.viewer.show_find()
        QTest.keyClick(self.viewer.txt_find, Qt.Key.Key_Escape)
        self.assertTrue(self.viewer.find_bar.isHidden())
        self.assertTrue(self.viewer.isVisible())

    def test_enter_in_the_field_finds_rather_than_pressing_a_button(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        self.viewer.show()
        self.viewer.show_find()
        self.viewer.txt_find.setText("beta")
        QTest.keyClick(self.viewer.txt_find, Qt.Key.Key_Return)
        self.assertEqual(self.selected(), "beta")
        self.assertTrue(self.viewer.isVisible())

    def test_the_label_counts_the_matches(self):
        self.viewer.txt_find.setText("beta")
        self.viewer.find()
        self.assertEqual(self.viewer.lbl_find.text(), "1 of 2")
        self.viewer.find()
        self.assertEqual(self.viewer.lbl_find.text(), "2 of 2")

    def test_the_bar_has_its_own_close_button(self):
        self.viewer.show()
        self.viewer.show_find()
        self.viewer.btn_close_find.click()
        self.assertTrue(self.viewer.find_bar.isHidden())
        self.assertTrue(self.viewer.isVisible())

    def test_escape_in_the_text_closes_the_bar_before_the_window(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        self.viewer.show()
        self.viewer.show_find()
        self.viewer.view.setFocus()
        QTest.keyClick(self.viewer.view, Qt.Key.Key_Escape)
        self.assertTrue(self.viewer.find_bar.isHidden())
        self.assertTrue(self.viewer.isVisible())

    def test_find_is_in_the_edit_menu(self):
        self.viewer.show()
        self.viewer.act_find.trigger()
        self.assertFalse(self.viewer.find_bar.isHidden())


class TestMenus(unittest.TestCase):
    def test_reload_is_in_the_file_menu_when_there_is_something_to_reload(self):
        calls = []
        viewer = TextDialog("t", "x", on_refresh=lambda: calls.append(1), auto_refresh=False)
        self.addCleanup(viewer.deleteLater)
        viewer.act_reload.trigger()
        self.assertEqual(calls, [1])
        self.assertFalse(hasattr(viewer, "chk_auto_refresh"))

    def test_without_a_source_reload_is_greyed_out(self):
        viewer = TextDialog("t", "x")
        self.addCleanup(viewer.deleteLater)
        self.assertFalse(viewer.act_reload.isEnabled())

    def test_wrap_can_be_turned_off(self):
        from PyQt6.QtWidgets import QPlainTextEdit

        viewer = TextDialog("t", "x")
        self.addCleanup(viewer.deleteLater)
        viewer.act_wrap.setChecked(False)
        self.assertEqual(viewer.view.lineWrapMode(), QPlainTextEdit.LineWrapMode.NoWrap)


class TestFollowingTheEnd(unittest.TestCase):
    """A refresh used to jump to the end every time, so with auto-refresh on
    nothing further up a log could be read."""

    LINES = "".join(f"line {i}\n" for i in range(500))

    def setUp(self):
        self.viewer = TextDialog("t", "", on_refresh=lambda: None, auto_refresh=False)
        self.addCleanup(self.viewer.deleteLater)
        self.viewer.resize(400, 200)
        self.viewer.show()
        self.bar = self.viewer.view.verticalScrollBar()

    def test_following_keeps_the_end_in_view(self):
        self.viewer.set_text(self.LINES)
        self.assertEqual(self.bar.value(), self.bar.maximum())
        self.viewer.set_text(self.LINES + "line 500\n")
        self.assertEqual(self.bar.value(), self.bar.maximum())

    def test_scrolling_up_stops_following_and_a_refresh_keeps_the_place(self):
        self.viewer.set_text(self.LINES)
        self.bar.setValue(100)
        self.assertFalse(self.viewer.chk_follow.isChecked())
        self.viewer.set_text(self.LINES + "line 500\nline 501\n")
        self.assertEqual(self.bar.value(), 100)

    def test_scrolling_back_to_the_end_follows_again(self):
        self.viewer.set_text(self.LINES)
        self.bar.setValue(100)
        self.bar.setValue(self.bar.maximum())
        self.assertTrue(self.viewer.chk_follow.isChecked())

    def test_a_result_file_opens_at_the_top(self):
        tmp = tempfile.mkdtemp(prefix="textfollow_")
        path = os.path.join(tmp, "result.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.LINES)
        viewer = jobs_dialog.show_text_window(path)
        self.addCleanup(viewer.close)
        self.assertEqual(viewer.view.verticalScrollBar().value(), 0)
        self.assertFalse(viewer.chk_follow.isChecked())
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("appended\n")
        viewer.act_reload.trigger()
        self.assertIn("appended", viewer.view.toPlainText())
        self.assertEqual(viewer.view.verticalScrollBar().value(), 0)


if __name__ == "__main__":
    unittest.main()
