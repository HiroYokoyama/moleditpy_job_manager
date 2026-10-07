"""Force run and re-check as the user meets them: the wizard's box, the
monitor's menu, and what the service does with a re-check's answer."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager import remote_runner  # noqa: E402
from job_manager.models import (  # noqa: E402
    MODE_RUNNER,
    SCHEDULER_SHELL,
    SENTINEL_NAME,
    STATE_DONE,
    STATE_LOST,
    STATE_PENDING,
    STATE_RUNNING,
    Job,
)
from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .fakes import make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402
from .test_force_and_recheck import FileTransport  # noqa: E402


class GuiCase(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.box = make_host(
            id="box",
            name="box",
            scheduler=SCHEDULER_SHELL,
            concurrency_mode=MODE_RUNNER,
        )
        self.store.add_host(self.box)

    def submit_dialog(self, host) -> SubmitDialog:
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.cmb_host.setCurrentIndex(dialog.cmb_host.findData(host.id))
        return dialog


class TestTheWizardsForceBox(GuiCase):
    def test_offered_where_the_plugin_keeps_the_queue(self):
        dialog = self.submit_dialog(self.box)
        self.assertFalse(dialog.chk_force.isHidden())

    def test_not_offered_on_a_cluster(self):
        dialog = self.submit_dialog(self.host)
        self.assertTrue(dialog.chk_force.isHidden())
        self.assertFalse(dialog.force_requested())

    def test_switching_to_a_cluster_unticks_it(self):
        dialog = self.submit_dialog(self.box)
        dialog.chk_force.setChecked(True)
        dialog.cmb_host.setCurrentIndex(dialog.cmb_host.findData(self.host.id))
        self.assertFalse(dialog.chk_force.isChecked())

    def test_forcing_drops_chaining_and_a_start_time(self):
        self.store.add_job(Job(name="ahead", host_id=self.box.id, state=STATE_RUNNING))
        dialog = self.submit_dialog(self.box)
        dialog.chk_start_at.setChecked(True)
        dialog.chk_force.setChecked(True)
        self.assertFalse(dialog.chk_start_at.isChecked())
        self.assertFalse(dialog.chk_start_at.isEnabled())
        self.assertFalse(dialog.chain_requested())
        self.assertIn("ahead of", dialog.lbl_chain.text())

    def test_the_submission_carries_it(self):
        dialog = self.submit_dialog(self.box)
        dialog.add_files([self.make_input()])
        dialog.chk_force.setChecked(True)
        with patch.object(self.service, "submit") as submit:
            dialog._submit()
        self.assertTrue(submit.call_args.kwargs["force_run"])
        self.assertIsNone(submit.call_args.kwargs["after_job"])


class TestTheServicesRefusals(GuiCase):
    def queued(self, **fields) -> Job:
        job = Job(name="q", host_id=self.box.id, scheduler=SCHEDULER_SHELL, **fields)
        job.remote_job_id = remote_runner.entry_name(1, job.id)
        self.store.add_job(job)
        return job

    def test_a_waiting_helper_job_can_be_forced(self):
        self.assertEqual(self.service.force_refusal(self.queued(state=STATE_PENDING)), "")

    def test_a_running_job_cannot(self):
        self.assertIn("RUNNING", self.service.force_refusal(self.queued(state=STATE_RUNNING)))

    def test_a_cluster_job_cannot(self):
        job = Job(name="c", host_id=self.host.id, state=STATE_PENDING, remote_job_id="4242")
        self.store.add_job(job)
        self.assertIn("helper queue", self.service.force_refusal(job))

    def test_a_job_waiting_for_an_unfinished_one_cannot(self):
        first = self.queued(state=STATE_RUNNING)
        second = self.queued(state=STATE_PENDING, after_job_id=first.id)
        self.assertIn("waits for", self.service.force_refusal(second))

    def test_forcing_records_it_and_says_so(self):
        job = self.queued(state=STATE_PENDING)
        transport = FileTransport(self.box).when("set -C", stdout="forced\n")
        self.service.transport_for = lambda host: transport
        messages = []
        self.service.message.connect(messages.append)

        self.service.force_run(job)

        self.assertTrue(job.force_run)
        self.assertTrue(any("ahead of the queue" in m for m in messages))


class TestRecheckingThroughTheService(GuiCase):
    def lost_job(self, **fields) -> Job:
        job = Job(
            name="lostjob",
            host_id=self.host.id,
            remote_dir="~/runs/r1",
            remote_job_id="4242",
            state=STATE_LOST,
            auto_download=False,
            finished_at=100.0,
            **fields,
        )
        self.store.add_job(job)
        return job

    def recheck(self, job, files, **rules):
        transport = FileTransport(self.host, files)
        for substring, stdout in rules.items():
            transport.when(substring, stdout=stdout)
        self.service.transport_for = lambda host: transport
        reports = []
        self.service.recheck(job, on_done=reports.append)
        return reports[0]

    def test_a_finished_job_is_corrected_and_announced(self):
        job = self.lost_job()
        finished = []
        self.service.job_finished.connect(lambda job_id, state: finished.append(state))

        report = self.recheck(job, {f"~/runs/r1/{SENTINEL_NAME}": "0"})

        self.assertTrue(report["changed"])
        self.assertEqual(job.state, STATE_DONE)
        self.assertEqual(job.rc, 0)
        self.assertEqual(finished, [STATE_DONE])
        # When it ended is when it was first seen gone, not now.
        self.assertEqual(job.finished_at, 100.0)

    def test_one_still_in_the_queue_is_polled_again(self):
        job = self.lost_job()
        self.recheck(job, {}, squeue="4242 RUNNING\n")
        self.assertEqual(job.state, STATE_RUNNING)
        self.assertEqual(job.finished_at, 0.0)

    def test_with_no_evidence_nothing_changes(self):
        job = self.lost_job()
        report = self.recheck(job, {})
        self.assertFalse(report["changed"])
        self.assertEqual(job.state, STATE_LOST)

    def test_only_a_lost_job_is_rechecked(self):
        job = self.lost_job()
        job.state = STATE_DONE
        errors = []
        self.service.recheck(job, on_error=errors.append)
        self.assertIn("only a LOST job", errors[0])


class TestTheMonitorsMenu(GuiCase):
    def test_the_recheck_report_window_shows_the_evidence(self):
        from job_manager.jobs_dialog import JobsDialog

        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        with patch("job_manager.jobs_dialog.QMessageBox.information") as shown:
            dialog._show_recheck(
                {
                    "changed": False,
                    "state": STATE_LOST,
                    "sentinel": "MISSING",
                    "runner_status": "",
                    "files": ["mol.out"],
                }
            )
        text = shown.call_args.args[2]
        self.assertIn("Still no sign", text)
        self.assertIn("mol.out", text)


if __name__ == "__main__":
    unittest.main()
