"""Submission, monitor, and saved presets expose helper queue priorities."""

from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402

from job_manager.job_table import COLUMNS  # noqa: E402
from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import MODE_RUNNER, SCHEDULER_SHELL, STATE_PENDING, Job, SubmitPreset  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .test_dialogs import DialogTestCase  # noqa: E402


class TestPriorityUi(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.host.scheduler = SCHEDULER_SHELL
        self.host.concurrency_mode = MODE_RUNNER

    def test_submission_priority_round_trips_and_only_applies_to_helpers(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog._apply_preset(SubmitPreset(priority=-30))
        assert dialog.collect_preset().priority == -30
        assert not dialog.spin_priority.isHidden()
        self.host.scheduler = "slurm"
        dialog._update_force_row()
        assert dialog.spin_priority.isHidden()
        assert dialog.collect_preset().priority == 0

    def test_table_and_action_display_and_change_priority(self):
        job = Job(
            id="p1",
            name="myjob",
            host_id=self.host.id,
            priority=2,
            remote_job_id="job_0001_p1.sh",
            state=STATE_PENDING,
        )
        self.store.add_job(job)
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.table.selectRow(0)
        cell = dialog.model.index(0, COLUMNS.index("Priority"))
        assert cell.data(Qt.ItemDataRole.UserRole) == 2
        assert dialog.job_actions["priority"].isEnabled()
        with (
            patch("job_manager.jobs_dialog.QInputDialog.getInt", return_value=(-12, True)),
            patch.object(self.service, "set_priority") as update,
        ):
            dialog._priority_selected()
        update.assert_called_once_with(job, -12, owner=dialog)

    def test_service_updates_saved_job_and_resubmit_snapshot_after_host_confirms(self):
        job = Job(
            id="p1", host_id=self.host.id, state=STATE_PENDING, remote_job_id="job_0001_p1.sh"
        )
        self.store.add_job(job)
        self.transport.when("echo updated", stdout="updated\n")
        self.service.set_priority(job, 25)
        assert job.priority == job.preset["priority"] == 25
        self.store.reload_jobs()
        assert self.store.jobs[job.id].priority == 25

    def test_service_keeps_record_when_job_leaves_queue(self):
        job = Job(
            id="p1",
            host_id=self.host.id,
            state=STATE_PENDING,
            remote_job_id="job_0001_p1.sh",
            priority=2,
        )
        self.store.add_job(job)
        self.transport.when("echo updated", stdout="notqueued\n")
        completed = []
        self.service.set_priority(
            job, 25, on_done=lambda current, updated: completed.append(updated)
        )
        assert job.priority == 2
        assert completed == [False]
