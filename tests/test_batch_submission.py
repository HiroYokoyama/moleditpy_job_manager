"""Dropping several files at once: one job by default, a batch with Shift.

A plain multi-file drop is one job's inputs (a Gaussian job with a companion
checkpoint, say). A Shift-held drop selects the wizard's "separate jobs" choice.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import QCoreApplication, QMimeData, QUrl, Qt  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402

from job_manager.input_names import primary_first  # noqa: E402
from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .test_dialogs import DialogTestCase  # noqa: E402


#: QDropEvent does not take ownership of the QMimeData pointer it is given --
#: the caller has to keep it alive for as long as the event might be read, and
#: a QMimeData built and dropped inside a helper function is freed on the C++
#: side the moment Python's reference count hits zero, which crashed every
#: test here with an access violation the first time mimeData() was read.
_MIME_KEEPALIVE = []


def make_drop_event(paths, modifiers=Qt.KeyboardModifier.NoModifier):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    _MIME_KEEPALIVE.append(mime)
    return QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        modifiers,
    )


class TestTheSubmitDialogsBatchCheckbox(DialogTestCase):
    def dialog(self) -> SubmitDialog:
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_hidden_with_one_file(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp")])
        self.assertFalse(dialog.chk_batch.isVisible())

    def test_shown_with_several_files(self):
        dialog = self.dialog()
        dialog.show()
        dialog.add_files([self.make_input("a.inp"), self.make_input("b.inp")])
        self.assertTrue(dialog.chk_batch.isVisible())

    def test_a_plain_drop_of_several_files_leaves_it_unticked(self):
        dialog = self.dialog()
        paths = [self.make_input("a.inp"), self.make_input("b.inp")]
        dialog.dropEvent(make_drop_event(paths))
        self.assertFalse(dialog.chk_batch.isChecked())

    def test_a_shift_drop_of_several_files_ticks_it(self):
        dialog = self.dialog()
        paths = [self.make_input("a.inp"), self.make_input("b.inp")]
        dialog.dropEvent(make_drop_event(paths, Qt.KeyboardModifier.ShiftModifier))
        self.assertTrue(dialog.chk_batch.isChecked())

    def test_a_plain_drop_puts_the_input_ahead_of_its_companion(self):
        dialog = self.dialog()
        checkpoint = self.make_input("mol.gbw")
        deck = self.make_input("mol.inp")
        dialog.dropEvent(make_drop_event([checkpoint, deck]))
        self.assertEqual(dialog.selected_files(), [deck, checkpoint])

    def test_a_plain_drop_submits_one_job_with_every_file(self):
        dialog = self.dialog()
        dialog.dropEvent(make_drop_event([self.make_input("a.gbw"), self.make_input("a.inp")]))
        dialog.txt_command.setText("orca {input} > {stem}.out")

        dialog._submit()

        self.assertEqual(len(self.store.jobs), 1)
        job = next(iter(self.store.jobs.values()))
        self.assertEqual(len(job.input_files), 2)
        self.assertTrue(job.input_files[0].endswith("a.inp"))

    def test_a_single_file_drop_does_not_touch_the_checkbox(self):
        dialog = self.dialog()
        dialog.dropEvent(make_drop_event([self.make_input("a.inp")]))
        self.assertFalse(dialog.chk_batch.isChecked())

    def test_the_file_picker_never_ticks_it(self):
        # A deliberate multi-select from a dialog is "these files are one
        # job's inputs", not "each of these is its own calculation".
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp"), self.make_input("b.inp")])
        self.assertFalse(dialog.chk_batch.isChecked())

    def test_working_on_the_host_disables_it(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp"), self.make_input("b.inp")])
        dialog.chk_batch.setChecked(True)
        dialog.box_remote.setChecked(True)
        self.assertFalse(dialog.chk_batch.isEnabled())
        self.assertFalse(dialog.chk_batch.isChecked())

    def test_ticking_it_turns_off_chaining(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp"), self.make_input("b.inp")])
        dialog.chk_chain.setChecked(True)
        dialog.chk_batch.setChecked(True)
        self.assertFalse(dialog.chk_chain.isChecked())

    def test_the_submit_button_counts_the_jobs(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp"), self.make_input("b.inp")])
        dialog.chk_batch.setChecked(True)
        self.assertIn("2", dialog.btn_submit.text())
        self.assertIn("2 separate jobs", dialog.chk_batch.text())
        dialog.chk_batch.setChecked(False)
        self.assertEqual(dialog.btn_submit.text(), "Submit 1 Job (2 files)")
        self.assertTrue(dialog.radio_one_job.isChecked())
        self.assertIn("all 2 files", dialog.radio_one_job.text())

    def test_the_submit_button_stays_plain_for_one_file(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp")])
        self.assertEqual(dialog.btn_submit.text(), "Submit")

    def test_the_preview_names_the_file_input_will_be(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp"), self.make_input("a.gbw")])
        dialog._refresh_preview()
        self.assertIn("a.inp", dialog.lbl_preview_hint.text())


class TestSubmittingABatch(DialogTestCase):
    def dialog(self) -> SubmitDialog:
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_each_file_becomes_its_own_job(self):
        dialog = self.dialog()
        files = [self.make_input("a.inp"), self.make_input("b.inp"), self.make_input("c.inp")]
        dialog.prefill(files=files, batch=True)
        dialog.txt_command.setText("orca {input} > {stem}.out")

        dialog._submit()

        names = sorted(job.name for job in self.store.jobs.values())
        self.assertEqual(names, ["a", "b", "c"])

    def test_each_job_carries_only_its_own_file(self):
        dialog = self.dialog()
        files = [self.make_input("a.inp"), self.make_input("b.inp")]
        dialog.prefill(files=files, batch=True)
        dialog.txt_command.setText("orca {input} > {stem}.out")

        dialog._submit()

        for job in self.store.jobs.values():
            self.assertEqual(len(job.input_files), 1)
            self.assertTrue(job.input_files[0].endswith(f"{job.name}.inp"))

    def test_the_dialog_closes_once(self):
        dialog = self.dialog()
        files = [self.make_input("a.inp"), self.make_input("b.inp")]
        dialog.prefill(files=files, batch=True)
        dialog.txt_command.setText("orca {input} > {stem}.out")
        accepted = []
        dialog.accept = lambda: accepted.append(1)

        dialog._submit()

        self.assertEqual(accepted, [1])

    def test_unticking_batch_submits_one_job_with_every_file(self):
        dialog = self.dialog()
        files = [self.make_input("a.inp"), self.make_input("b.inp")]
        dialog.prefill(files=files, batch=False)
        dialog.txt_command.setText("orca {input} > {stem}.out")

        dialog._submit()

        self.assertEqual(len(self.store.jobs), 1)
        job = next(iter(self.store.jobs.values()))
        self.assertEqual(len(job.input_files), 2)

    def test_batch_plus_work_already_on_the_host_is_refused(self):
        # The checkbox itself is disabled the moment "work already on the
        # host" is ticked -- _update_batch_row() re-syncs it on every toggle,
        # so the UI cannot actually reach this combination. The guard in
        # _submit() is the backstop for a caller that drives the model
        # directly (a test, a future entry point), proven the same way.
        dialog = self.dialog()
        files = [self.make_input("a.inp"), self.make_input("b.inp")]
        dialog.prefill(files=files, batch=False)
        dialog.txt_command.setText("orca {input} > {stem}.out")
        dialog.box_remote.setChecked(True)
        dialog.txt_remote_dir.setText("~/staged")
        dialog._batch_active = lambda: True

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "job_manager.submit_dialog.QMessageBox.warning"
        ) as warned:
            dialog._submit()

        warned.assert_called_once()
        self.assertEqual(len(self.store.jobs), 0)

    def test_a_batch_can_be_chained_when_asked_explicitly(self):
        dialog = self.dialog()
        files = [self.make_input("a.inp"), self.make_input("b.inp"), self.make_input("c.inp")]
        dialog.prefill(files=files, batch=True)
        dialog.txt_command.setText("orca {input} > {stem}.out")
        dialog.chk_chain.setEnabled(True)
        dialog.chk_chain.setVisible(True)
        dialog.chk_chain.setChecked(True)

        dialog._submit()

        jobs_by_name = {job.name: job for job in self.store.jobs.values()}
        # b chains behind a, c chains behind b: each batch submission's
        # predecessor is read fresh, and the previous one in this same loop
        # is by then the newest active job on the host.
        self.assertEqual(jobs_by_name["b"].after_job_id, jobs_by_name["a"].id)
        self.assertEqual(jobs_by_name["c"].after_job_id, jobs_by_name["b"].id)


class TestTheMonitorsDropHandler(DialogTestCase):
    def dialog(self) -> JobsDialog:
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_a_plain_drop_of_several_files_opens_the_wizard_as_one_job(self):
        dialog = self.dialog()
        called = {}
        dialog.open_submit_dialog = lambda **kwargs: called.update(kwargs)
        checkpoint = self.make_input("mol.gbw")
        deck = self.make_input("mol.inp")
        dialog.dropEvent(make_drop_event([checkpoint, deck]))
        QCoreApplication.processEvents()

        self.assertFalse(called.get("batch"))
        self.assertEqual(called.get("files"), [deck, checkpoint])

    def test_a_shift_drop_of_several_files_opens_it_batched(self):
        dialog = self.dialog()
        called = {}
        dialog.open_submit_dialog = lambda **kwargs: called.update(kwargs)
        paths = [self.make_input("a.inp"), self.make_input("b.inp")]
        dialog.dropEvent(make_drop_event(paths, Qt.KeyboardModifier.ShiftModifier))
        QCoreApplication.processEvents()

        self.assertTrue(called.get("batch"))

    def test_the_drop_returns_before_the_wizard_opens(self):
        # The drag source blocks until dropEvent returns, and the wizard is
        # modal: opening it inside the drop froze Explorer until it closed.
        dialog = self.dialog()
        opened = MagicMock()
        dialog.open_submit_dialog = opened
        dialog.dropEvent(make_drop_event([self.make_input("a.inp")]))
        opened.assert_not_called()
        QCoreApplication.processEvents()
        opened.assert_called_once()

    def test_a_single_file_drop_is_never_batched(self):
        dialog = self.dialog()
        called = {}
        dialog.open_submit_dialog = lambda **kwargs: called.update(kwargs)
        dialog.dropEvent(make_drop_event([self.make_input("a.inp")]))
        QCoreApplication.processEvents()

        self.assertFalse(called.get("batch"))

    def test_a_reconstructed_list_still_refuses_a_batch_drop(self):
        dialog = self.dialog()
        path = os.path.join(self.tmp, "rebuilt.pmejbs")
        self.store.write_job_list(path, [], reconstructed=True)
        self.store.use_jobs_file(path)
        opened = MagicMock()
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "job_manager.jobs_dialog.QMessageBox.information"
        ) as told:
            dialog.open_submit_dialog(
                files=[self.make_input("a.inp"), self.make_input("b.inp")], batch=True
            )
        told.assert_called_once()
        opened.assert_not_called()


class TestPrimaryFirst(unittest.TestCase):
    def test_the_input_goes_ahead_of_a_checkpoint_and_a_structure(self):
        self.assertEqual(
            primary_first(["mol.gbw", "mol.xyz", "MOL.INP"]), ["MOL.INP", "mol.xyz", "mol.gbw"]
        )

    def test_a_script_drives_the_input_dropped_with_it(self):
        self.assertEqual(primary_first(["mol.inp", "run.sh"]), ["run.sh", "mol.inp"])

    def test_files_of_equal_rank_keep_their_order(self):
        self.assertEqual(primary_first(["b.inp", "a.inp"]), ["b.inp", "a.inp"])


class TestAJobListOpenedFromAMainWindowDrop(unittest.TestCase):
    def test_the_drop_returns_before_the_question_is_asked(self):
        import job_manager

        window = MagicMock()
        context = MagicMock()
        context.get_window.return_value = window
        with (
            patch.object(job_manager, "_context", context),
            patch.object(job_manager, "show_monitor"),
        ):
            self.assertTrue(job_manager.handle_dropped_file("/tmp/jobs.pmejdb"))
            window.open_job_list.assert_not_called()
            QCoreApplication.processEvents()
        window.open_job_list.assert_called_once_with("/tmp/jobs.pmejdb")


if __name__ == "__main__":
    unittest.main()
