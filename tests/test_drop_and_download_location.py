"""Dropping an input file, and where its results land.

Two ends of the same workflow. Dropping onto a Job Manager window is
unambiguous -- it is the job window -- which is why input extensions are not
claimed application-wide: that would take ``.inp`` and ``.xyz`` away from being
*opened*, which is what dropping one on the main window usually means.

Results then land beside the input by default, because that is the directory
the user is already working in.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pytest

from job_manager.store import JobStore

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402
from PyQt6.QtCore import QMimeData, QPoint, QPointF, QUrl, Qt  # noqa: E402
from PyQt6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402


def drop_event(paths):
    """A real drop event carrying local file URLs.

    The QMimeData is returned alongside on purpose: a QDropEvent does **not**
    own it, so letting it be collected leaves Qt reading freed memory.
    """
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    event = QDropEvent(
        QPointF(1.0, 1.0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    return event, mime


def drag_enter_event(paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
    event = QDragEnterEvent(
        QPoint(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    return event, mime


class DropTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drop_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(self.tmp)
        self.service = JobService(store=self.store)
        self.addCleanup(self.service.shutdown)

    def make_file(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("! B3LYP\n")
        return path


class TestDroppingOnTheMonitor(DropTestCase):
    def _monitor(self) -> JobsDialog:
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.close)
        return dialog

    def test_an_input_file_opens_the_submit_wizard_prefilled(self):
        dialog = self._monitor()
        path = self.make_file("mol.inp")
        event, _mime = drop_event([path])

        with patch.object(dialog, "open_submit_dialog") as opened:
            dialog.dropEvent(event)

        opened.assert_called_once()
        self.assertEqual(opened.call_args.kwargs["files"], [path])

    def test_a_job_list_still_opens_as_a_job_list(self):
        # The existing behaviour must not be swallowed by the new one.
        dialog = self._monitor()
        path = self.make_file("saved.pmejbs")
        event, _mime = drop_event([path])

        with patch.object(dialog, "open_job_list") as opened:
            with patch.object(dialog, "open_submit_dialog") as submitted:
                dialog.dropEvent(event)

        opened.assert_called_once_with(path)
        submitted.assert_not_called()

    def test_several_inputs_are_all_offered(self):
        dialog = self._monitor()
        paths = [self.make_file("a.inp"), self.make_file("b.xyz")]
        event, _mime = drop_event(paths)

        with patch.object(dialog, "open_submit_dialog") as opened:
            dialog.dropEvent(event)

        self.assertEqual(opened.call_args.kwargs["files"], paths)

    def test_a_drag_of_files_is_accepted(self):
        dialog = self._monitor()
        event, _mime = drag_enter_event([self.make_file("mol.inp")])

        dialog.dragEnterEvent(event)

        self.assertTrue(event.isAccepted())

    def test_a_drag_of_nothing_local_is_refused(self):
        dialog = self._monitor()
        mime = QMimeData()
        mime.setText("just some text")
        event = QDragEnterEvent(
            QPoint(1, 1),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

        dialog.dragEnterEvent(event)

        self.assertFalse(event.isAccepted())

    def test_a_directory_is_not_an_input_file(self):
        dialog = self._monitor()
        event, _mime = drop_event([self.tmp])

        with patch.object(dialog, "open_submit_dialog") as opened:
            dialog.dropEvent(event)

        opened.assert_not_called()


class TestDroppingOnTheWizard(DropTestCase):
    def _wizard(self) -> SubmitDialog:
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        return dialog

    def test_a_dropped_file_is_added_to_the_list(self):
        dialog = self._wizard()
        path = self.make_file("mol.inp")
        event, _mime = drop_event([path])

        dialog.dropEvent(event)

        self.assertEqual(dialog.selected_files(), [path])

    def test_the_job_is_named_after_the_first_file(self):
        dialog = self._wizard()
        event, _mime = drop_event([self.make_file("benzene.inp")])

        dialog.dropEvent(event)

        self.assertEqual(dialog.txt_job_name.text(), "benzene")

    def test_the_same_file_twice_is_added_once(self):
        dialog = self._wizard()
        path = self.make_file("mol.inp")
        for _ in range(2):
            event, _mime = drop_event([path])
            dialog.dropEvent(event)

        self.assertEqual(dialog.selected_files(), [path])

    def test_a_dropped_extension_suggests_its_command(self):
        # Adding a file has to do the follow-up work, or it is just listed.
        dialog = self._wizard()
        dialog.txt_command.setText("")
        event, _mime = drop_event([self.make_file("mol.xyz")])

        dialog.dropEvent(event)

        self.assertIn("xtb", dialog.txt_command.text())

    def test_an_ambiguous_extension_is_left_for_the_user(self):
        # .inp is ORCA, CP2K and GAMESS; guessing one would be worse than
        # guessing none.
        dialog = self._wizard()
        dialog.txt_command.setText("")
        event, _mime = drop_event([self.make_file("mol.inp")])

        dialog.dropEvent(event)

        self.assertEqual(dialog.txt_command.text(), "")


class TestWhereResultsLand(DropTestCase):
    def test_beside_the_input_by_default(self):
        job_dir = os.path.join(self.tmp, "project")
        os.makedirs(job_dir)
        path = os.path.join(job_dir, "mol.inp")
        open(path, "w").close()

        self.assertEqual(self.service._local_dir_for("mol", [path]), job_dir)

    def test_the_download_root_when_switched_off(self):
        self.store.prefs["download_beside_input"] = False
        path = self.make_file("mol.inp")

        chosen = self.service._local_dir_for("mol", [path])

        self.assertTrue(chosen.startswith(self.store.download_root()))

    def test_the_download_root_when_there_is_no_input_to_sit_beside(self):
        chosen = self.service._local_dir_for("mol", [])

        self.assertTrue(chosen.startswith(self.store.download_root()))

    def test_a_vanished_input_directory_falls_back(self):
        # Resubmitting a job whose input has since been moved or deleted.
        chosen = self.service._local_dir_for("mol", [os.path.join(self.tmp, "gone", "mol.inp")])

        self.assertTrue(chosen.startswith(self.store.download_root()))

    def test_the_preference_is_remembered(self):
        self.store.set_pref("download_beside_input", False)

        self.assertFalse(JobStore(self.tmp).get_pref("download_beside_input", True))


if __name__ == "__main__":
    unittest.main()
