"""A waiting job should show useful details instead of trying a missing log."""

from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets")

from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import Job, STATE_PENDING, STATE_RUNNING, STATE_UPLOADING  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TestWaitingJobDoubleClick(DialogTestCase):
    def test_waiting_and_uploading_jobs_open_details_without_remote_reads(self):
        job = Job(id="wait1", host_id=self.host.id, state=STATE_PENDING, priority=7)
        self.store.add_job(job)
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.table.selectRow(0)
        with (
            patch.object(dialog, "_show_details") as details,
            patch.object(dialog, "_tail_selected") as tail,
        ):
            for state in (STATE_PENDING, STATE_UPLOADING):
                job.state = state
                dialog._open_double_clicked()
            assert details.call_count == 2
            tail.assert_not_called()
        text = dialog._describe(job)
        assert "has not started yet" in text
        assert "Priority" in text and "7" in text

    def test_running_jobs_still_open_their_log(self):
        self.store.add_job(
            Job(
                id="run1",
                host_id=self.host.id,
                state=STATE_RUNNING,
                remote_dir="/work/myjob",
                log_file="job.log",
            )
        )
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.table.selectRow(0)
        with (
            patch.object(dialog, "_show_details") as details,
            patch.object(dialog, "_tail_selected") as tail,
        ):
            dialog._open_double_clicked()
            tail.assert_called_once()
            details.assert_not_called()

    def test_tail_file_toolbar_reuses_the_existing_action(self):
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        actions = dialog.job_toolbar.actions()
        assert (
            actions.index(dialog.job_actions["tail_file"])
            == actions.index(dialog.job_actions["tail"]) + 1
        )
        assert dialog.btn_tail_file.defaultAction() is dialog.job_actions["tail_file"]
        assert dialog.btn_tail_file.text() == "Tail File…"
