"""The table and Details expose manual/dependency gates and safe queue edits."""

from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt
from job_manager.details_dialog import JobDetailsDialog
from job_manager.jobs_dialog import JobsDialog
from job_manager.job_table import COLUMNS
from job_manager.models import Job, STATE_HELD
from .test_dialogs import DialogTestCase


class TestHoldDetails(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.host.scheduler = "shell"
        self.host.concurrency_mode = "runner"
        self.parent_job = self.store.add_job(
            Job(id="parent", host_id=self.host.id, name="Previous check", state="RUNNING")
        )
        self.job = self.store.add_job(
            Job(
                id="child",
                host_id=self.host.id,
                name="Next check",
                state="PENDING",
                remote_job_id="job_0002_child.sh",
                after_job_id=self.parent_job.id,
                priority=-5,
            )
        )
        self.monitor = JobsDialog(self.service)
        self.addCleanup(self.monitor.deleteLater)
        self.details = JobDetailsDialog(
            self.service,
            self.job,
            self.monitor._describe(self.job),
            "Details",
            describe=self.monitor._describe,
        )
        self.addCleanup(self.details.deleteLater)

    def test_after_and_manual_hold_have_one_state_with_distinct_reasons(self):
        model = self.monitor.model
        row = model.row_of(self.job.id)
        state = model.index(row, COLUMNS.index("State"))
        assert model.data(state, Qt.ItemDataRole.DisplayRole) == "HELD — After: Previous check"
        self.job.state = STATE_HELD
        self.service.job_updated.emit(self.job.id)
        assert model.data(state) == "HELD — Manual / After: Previous check"
        assert "Manual / After: Previous check" == self.details.lbl_hold.text()
        assert "Manual hold" in self.details.view.toPlainText()
        self.parent_job.state = "DONE"
        self.service.job_updated.emit(self.parent_job.id)
        assert self.details.lbl_hold.text() == "Manual"

    def test_details_edits_priority_then_hold_and_release_without_losing_after(self):
        self.details.spin_priority.setValue(-100)
        with patch("job_manager.service.set_runner_priority", return_value=True) as operation:
            self.details.btn_priority.click()
        operation.assert_called_once()
        assert self.job.priority == -100
        assert "-100" in self.details.view.toPlainText()
        assert self.details.btn_hold.isEnabled()
        assert not self.details.btn_release.isEnabled()
        with patch("job_manager.service.set_runner_hold", return_value=True):
            self.details.btn_hold.click()
            assert self.job.state == STATE_HELD
            assert self.details.btn_release.isEnabled()
            assert not self.details.btn_hold.isEnabled()
            assert self.service.force_refusal(self.job)
            self.details.btn_release.click()
        assert self.job.state == "PENDING"
        assert self.job.after_job_id == self.parent_job.id
        assert self.job.priority == -100
        assert self.details.lbl_hold.text() == "After: Previous check"
        assert not self.details.btn_release.isEnabled()

    def test_queue_buttons_are_disabled_while_running_read_only_or_replaced(self):
        self.job.state = "RUNNING"
        self.service.job_updated.emit(self.job.id)
        assert not self.details.spin_priority.isEnabled()
        assert not self.details.btn_hold.isEnabled()
        assert not self.details.btn_release.isEnabled()
        self.job.state = "PENDING"
        self.details.read_only = True
        self.details._refresh_queue()
        assert not self.details.spin_priority.isEnabled()
        self.details.read_only = False
        self.store.jobs[self.job.id] = Job.from_dict(self.job.to_dict())
        self.service.jobs_changed.emit()
        assert not self.details.btn_hold.isEnabled()
        with patch("job_manager.service.set_runner_priority") as operation:
            self.details._apply_priority()
        operation.assert_not_called()

    def test_async_operation_disables_controls_and_shows_error_without_changing_record(self):
        captured = {}

        def delayed(job, value, **callbacks):
            captured.update(callbacks)

        with patch.object(self.service, "set_priority", side_effect=delayed):
            self.details.spin_priority.setValue(8)
            self.details.btn_priority.click()
            assert not self.details.btn_priority.isEnabled()
            assert not self.details.btn_hold.isEnabled()
            captured["on_error"]("connection lost")
        assert self.details.lbl_saved.text() == "connection lost"
        assert self.job.priority == -5
        assert self.details.spin_priority.value() == 8
        assert self.details.btn_hold.isEnabled()

    def test_inflight_poll_cannot_overwrite_completed_hold_or_release(self):
        poller = self.service.poller
        poller._poll_states[self.host.id] = {self.job.id: "PENDING"}
        self.job.state = STATE_HELD
        poller._on_poll_finished(self.host.id, {self.job.id: "RUNNING"})
        assert self.job.state == STATE_HELD
        poller._poll_states[self.host.id] = {self.job.id: STATE_HELD}
        self.job.state = "PENDING"
        poller._on_poll_finished(self.host.id, {self.job.id: STATE_HELD})
        assert self.job.state == "PENDING"

    def test_hold_completion_cannot_resurrect_a_cancelled_job(self):
        captured = {}

        def delayed(pool, work, **callbacks):
            captured.update(callbacks)

        with patch("job_manager.service.run_async", side_effect=delayed):
            self.service.set_hold(self.job, True)
        self.job.state = "CANCELLED"
        captured["on_success"](True)
        assert self.job.state == "CANCELLED"

    def test_host_card_and_counts_do_not_call_an_after_wrapper_running(self):
        from job_manager.host_widgets import HostCard
        from job_manager.presence import count_jobs

        self.parent_job.state = "PENDING"
        self.parent_job.submitted_at = 10
        self.job.state = "RUNNING"  # A direct-mode wrapper waiting for its predecessor.
        self.job.submitted_at = 20
        card = HostCard(self.host)
        self.addCleanup(card.deleteLater)
        card.show_jobs([self.parent_job, self.job])
        assert "held — After: Previous check" in card.lbl_job.toolTip()
        assert count_jobs(self.store) == {"running": 0, "waiting": 2, "blocked": 0}
        assert not card._running_job_labels

    def test_record_refresh_preserves_selection_and_queue_tooltip_names_the_hold(self):
        cursor = self.details.view.textCursor()
        cursor.setPosition(2)
        cursor.setPosition(8, cursor.MoveMode.KeepAnchor)
        self.details.view.setTextCursor(cursor)
        self.job.priority = 7
        self.service.job_updated.emit(self.job.id)
        assert self.details.view.textCursor().anchor() == 2
        assert self.details.view.textCursor().position() == 8
        model = self.monitor.model
        index = model.index(model.row_of(self.job.id), COLUMNS.index("State"))
        assert "Held: After: Previous check" in model.data(index, Qt.ItemDataRole.ToolTipRole)

    def _select_child(self):
        index = self.monitor.model.index(self.monitor.model.row_of(self.job.id), 0)
        self.monitor.table.selectRow(self.monitor.proxy.mapFromSource(index).row())

    def test_job_menu_holds_and_releases_the_selected_job(self):
        self._select_child()
        with patch("job_manager.service.set_runner_hold", return_value=True):
            self.monitor.job_actions["hold"].trigger()
            assert self.job.state == STATE_HELD
            assert "hold" not in self.monitor._busy_actions
            self.monitor.job_actions["release"].trigger()
        assert self.job.state == "PENDING"
        assert self.job.after_job_id == self.parent_job.id
        assert "release" not in self.monitor._busy_actions

    def test_job_menu_reports_a_raced_dispatch_and_transport_failure(self):
        from job_manager.transport.base import TransportError

        self._select_child()
        with patch("job_manager.service.set_runner_hold", return_value=False):
            self.monitor.job_actions["hold"].trigger()
        assert "no longer waiting" in self.monitor.txt_log.toPlainText()
        with patch(
            "job_manager.service.set_runner_hold", side_effect=TransportError("host unavailable")
        ):
            self.monitor.job_actions["hold"].trigger()
        assert "host unavailable" in self.monitor.txt_log.toPlainText()
        assert "hold" not in self.monitor._busy_actions
        assert self.job.state == "PENDING"

    def test_menu_and_details_respect_cancelled_password_prompt(self):
        self.monitor.table.clearSelection()
        with patch.object(self.service, "set_hold") as operation:
            self.monitor._hold_selected()
            self._select_child()
            with patch.object(self.monitor, "_has_credentials", return_value=False):
                self.monitor._hold_selected()
        operation.assert_not_called()
        with (
            patch("job_manager.credentials.ensure_password", return_value=False),
            patch.object(self.service, "set_priority") as operation,
        ):
            self.details._apply_priority()
        operation.assert_not_called()
        assert not self.details._queue_busy

    def test_service_refuses_invalid_hold_and_after_only_release(self):
        errors = []
        self.service.set_hold(self.job, "yes", on_error=errors.append)
        self.service.set_hold(self.job, False, on_error=errors.append)
        self.job.state = "RUNNING"
        self.service.set_hold(self.job, True, on_error=errors.append)
        assert len(errors) == 3
        assert "boolean" in errors[0]
        assert "After conditions remain" in errors[1]
        assert "Only waiting" in errors[2]

    def test_service_rejects_a_hold_reply_for_a_replaced_job_list(self):
        callbacks, errors = {}, []

        def delayed(pool, work, **handlers):
            callbacks.update(handlers)

        with patch("job_manager.service.run_async", side_effect=delayed):
            self.service.set_hold(self.job, True, on_error=errors.append)
        replacement = Job.from_dict(self.job.to_dict())
        self.store.jobs[self.job.id] = replacement
        callbacks["on_success"](True)
        assert "job list changed" in errors[0]
        assert replacement.state == "PENDING"


class TestJobServerQueuePosition(DialogTestCase):
    def test_state_column_and_details_name_the_position_only_while_waiting(self):
        job = self.store.add_job(
            Job(
                id="srv",
                host_id=self.host.id,
                name="Server job",
                state="PENDING",
                server_job_id="s1",
                server_queue_ahead=3,
            )
        )
        monitor = JobsDialog(self.service)
        self.addCleanup(monitor.deleteLater)
        model = monitor.model
        state = model.index(model.row_of(job.id), COLUMNS.index("State"))
        assert model.data(state, Qt.ItemDataRole.DisplayRole) == "PENDING (3 ahead)"
        assert "Queue position" in monitor._describe(job)
        job.server_queue_ahead = 0
        self.service.job_updated.emit(job.id)
        assert model.data(state) == "PENDING (next in line)"
        job.state = "RUNNING"
        self.service.job_updated.emit(job.id)
        assert model.data(state) == "RUNNING"
        assert "Queue position" not in monitor._describe(job)
