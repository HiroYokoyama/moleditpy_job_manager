"""The wizard's "Reuse another job's file" box.

structure_relay.py is tested on its own in test_structure_relay.py; this is
about wiring it into the dialog -- which jobs are offered (same host only),
when the box is allowed to be ticked, and that a real submission both
substitutes the tag and asks the service to copy the right file.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager.dialect import PRESENT  # noqa: E402
from job_manager.models import STATE_DONE  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .fakes import make_host, make_job  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TestOfferingSourceJobs(DialogTestCase):
    def dialog(self) -> SubmitDialog:
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_a_finished_job_on_this_host_is_offered(self):
        self.store.add_job(make_job(id="j1", name="opt", host_id=self.host.id, state=STATE_DONE))
        dialog = self.dialog()
        dialog.box_relay.setChecked(True)
        self.assertEqual(dialog.cmb_relay_source.count(), 1)

    def test_a_job_on_a_different_host_is_not_offered(self):
        other = make_host(id="other-host", name="zzz_other")
        self.store.add_host(other)
        self.store.add_job(make_job(id="j1", name="opt", host_id=other.id, state=STATE_DONE))
        dialog = self.dialog()
        dialog.box_relay.setChecked(True)
        self.assertEqual(dialog.cmb_relay_source.count(), 0)

    def test_a_still_running_job_is_offered(self):
        # Relaying from a job that has not finished yet is the point of
        # allowing active jobs at all: the new submission chains behind it
        # (see TestSubmittingWithARelay), so the copy is safe once it runs.
        from job_manager.models import STATE_RUNNING

        self.store.add_job(
            make_job(id="j1", name="running", host_id=self.host.id, state=STATE_RUNNING)
        )
        dialog = self.dialog()
        dialog.box_relay.setChecked(True)
        self.assertEqual(dialog.cmb_relay_source.count(), 1)

    def test_a_failed_job_is_not_offered(self):
        from job_manager.models import STATE_FAILED

        self.store.add_job(
            make_job(id="j1", name="failed", host_id=self.host.id, state=STATE_FAILED)
        )
        dialog = self.dialog()
        dialog.box_relay.setChecked(True)
        self.assertEqual(dialog.cmb_relay_source.count(), 0)

    def test_switching_host_re_filters_the_candidates(self):
        other = make_host(id="other-host", name="zzz_other")
        self.store.add_host(other)
        self.store.add_job(make_job(id="j1", name="here", host_id=self.host.id, state=STATE_DONE))
        self.store.add_job(make_job(id="j2", name="there", host_id=other.id, state=STATE_DONE))
        dialog = self.dialog()
        dialog.box_relay.setChecked(True)
        self.assertEqual(dialog.cmb_relay_source.itemText(0), "here")

        index = dialog.cmb_host.findData(other.id)
        dialog.cmb_host.setCurrentIndex(index)
        self.assertEqual(dialog.cmb_relay_source.count(), 1)
        self.assertEqual(dialog.cmb_relay_source.itemText(0), "there")


class TestTheBoxIsMutuallyExclusive(DialogTestCase):
    def dialog(self) -> SubmitDialog:
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_hidden_with_no_files(self):
        dialog = self.dialog()
        self.assertFalse(dialog.box_relay.isEnabled())

    def test_enabled_with_a_file(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("run.inp")])
        self.assertTrue(dialog.box_relay.isEnabled())

    def test_disabled_with_work_already_on_the_host(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("run.inp")])
        dialog.box_relay.setChecked(True)
        dialog.box_remote.setChecked(True)
        self.assertFalse(dialog.box_relay.isEnabled())
        self.assertFalse(dialog.box_relay.isChecked())

    def test_disabled_in_batch_mode(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp"), self.make_input("b.inp")])
        dialog.box_relay.setChecked(True)
        dialog.chk_batch.setChecked(True)
        self.assertFalse(dialog.box_relay.isEnabled())
        self.assertFalse(dialog.box_relay.isChecked())

    def test_ticking_it_turns_off_batch(self):
        dialog = self.dialog()
        dialog.add_files([self.make_input("a.inp"), self.make_input("b.inp")])
        dialog.chk_batch.setChecked(True)
        dialog.box_relay.setChecked(True)
        self.assertFalse(dialog.chk_batch.isChecked())


class TestSubmittingWithARelay(DialogTestCase):
    def dialog(self) -> SubmitDialog:
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def source_job(self, name="opt") -> None:
        self.store.add_job(
            make_job(
                id=f"{name}-id",
                name=name,
                host_id=self.host.id,
                state=STATE_DONE,
                remote_dir="/home/tester/jobs/opt_20260101",
            )
        )
        # require_remote_path's existence check, ahead of the actual copy --
        # the fake otherwise answers every "exists?" with a default MISSING.
        self.transport.when(f"{name}.chk", stdout=PRESENT + "\n")
        self.transport.when(f"{name}.xyz", stdout=PRESENT + "\n")

    def select_source(self, dialog: SubmitDialog, name: str) -> None:
        index = dialog.cmb_relay_source.findText(name)
        self.assertGreaterEqual(index, 0, f"{name} was not offered")
        dialog.cmb_relay_source.setCurrentIndex(index)

    def test_the_uploaded_file_has_the_tag_replaced(self):
        self.source_job()
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("%oldchk=[prevfile:.chk]\n# hi\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("g16 {input} > {stem}.log")
        dialog.box_relay.setChecked(True)
        self.select_source(dialog, "opt")

        dialog._submit()

        # What reached the host, not what the job says its input was: those are
        # deliberately different now. The substituted copy is uploaded; the job
        # still belongs to the file the user picked, so its results land beside
        # that one instead of in a temp folder.
        remote = next(path for path in self.transport.uploaded_text if path.endswith("run.inp"))
        self.assertEqual(self.transport.uploaded_text[remote], "%oldchk=opt.chk\n# hi\n")

    def test_the_job_records_the_users_own_file_not_the_scratch_copy(self):
        # _local_dir_for sits a job's results beside its input, so recording
        # the substituted copy put every relayed job's results in a temp
        # directory -- and left Resubmit pointing at a scratch path.
        self.source_job()
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("%oldchk=[prevfile:.chk]\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("g16 {input} > {stem}.log")
        dialog.box_relay.setChecked(True)
        self.select_source(dialog, "opt")

        dialog._submit()

        job = next(j for j in self.store.jobs.values() if j.id != "opt-id")
        self.assertEqual(job.input_files, [template])
        self.assertEqual(job.local_dir, os.path.dirname(template))

    def test_a_tag_left_in_with_the_box_unticked_is_refused(self):
        # Unticked, the tag went to the host verbatim and the program read a
        # filename of "[prevfile:.chk]" -- nothing failed until the calculation
        # did, an hour later on the cluster.
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("%oldchk=[prevfile:.chk]\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("g16 {input} > {stem}.log")
        self.assertFalse(dialog.box_relay.isChecked())

        with patch("job_manager.submit_dialog.QMessageBox.warning") as warned:
            dialog._submit()

        warned.assert_called_once()
        self.assertEqual(self.store.jobs, {})

    def test_the_original_file_on_disk_is_never_changed(self):
        self.source_job()
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("[prevfile:.xyz]\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("orca {input} > {stem}.out")
        dialog.box_relay.setChecked(True)
        self.select_source(dialog, "opt")
        dialog._submit()

        self.assertEqual(open(template, encoding="utf-8").read(), "[prevfile:.xyz]\n")

    def test_a_missing_tag_refuses_to_submit(self):
        self.source_job()
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("! opt\n* xyz 0 1\nO 0 0 0\n*\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("orca {input} > {stem}.out")
        dialog.box_relay.setChecked(True)
        self.select_source(dialog, "opt")

        with patch("job_manager.submit_dialog.QMessageBox.warning") as warned:
            dialog._submit()

        warned.assert_called_once()
        # The one job in the store is the relay source itself, set up by
        # self.source_job(); the refused submission adds none besides it.
        self.assertEqual(len(self.store.jobs), 1)

    def test_no_source_selected_refuses_to_submit(self):
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("[prevfile:.xyz]\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("orca {input} > {stem}.out")
        dialog.box_relay.setChecked(True)

        with patch("job_manager.submit_dialog.QMessageBox.warning") as warned:
            dialog._submit()

        warned.assert_called_once()
        self.assertEqual(len(self.store.jobs), 0)

    def test_the_service_is_asked_to_copy_the_resolved_files(self):
        self.source_job()
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("[prevfile:.chk] [prevfile:.xyz]\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("g16 {input} > {stem}.log")
        dialog.box_relay.setChecked(True)
        self.select_source(dialog, "opt")

        with patch.object(self.service, "submit", wraps=self.service.submit) as submit:
            dialog._submit()

        kwargs = submit.call_args.kwargs
        self.assertEqual(kwargs["relay_source_dir"], "/home/tester/jobs/opt_20260101")
        self.assertEqual(sorted(kwargs["relay_filenames"]), ["opt.chk", "opt.xyz"])

    def test_relaying_from_a_still_running_job_chains_behind_it(self):
        from job_manager.models import STATE_RUNNING

        self.store.add_job(
            make_job(
                id="opt-id",
                name="opt",
                host_id=self.host.id,
                state=STATE_RUNNING,
                remote_dir="/home/tester/jobs/opt_20260101",
            )
        )
        self.transport.when("opt.xyz", stdout=PRESENT + "\n")
        template = self.make_input("run.inp")
        with open(template, "w", encoding="utf-8") as handle:
            handle.write("[prevfile:.xyz]\n")

        dialog = self.dialog()
        dialog.add_files([template])
        dialog.txt_command.setText("orca {input} > {stem}.out")
        dialog.box_relay.setChecked(True)
        self.select_source(dialog, "opt")

        with patch.object(self.service, "submit", wraps=self.service.submit) as submit:
            dialog._submit()

        kwargs = submit.call_args.kwargs
        self.assertEqual(kwargs["after_job"].id, "opt-id")
        self.assertFalse(kwargs["chain_any"])


if __name__ == "__main__":
    unittest.main()
