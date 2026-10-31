"""Numeric priorities are available to API clients without bypassing the queue."""

from unittest.mock import Mock

import pytest

from job_manager.api_client import JobManagerClient, _submit_fields, build_parser
from job_manager.api_types import ApiError
from job_manager.models import MODE_RUNNER, SCHEDULER_SHELL, STATE_PENDING

from .test_api_core import ApiTestCase


class TestPriorityApi(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.host.scheduler = SCHEDULER_SHELL
        self.host.concurrency_mode = MODE_RUNNER

    def test_submit_records_priority(self):
        _, data = self.post("/jobs", **self.submit_body(priority=42))
        assert data["job"]["priority"] == 42
        assert self.service.submitted[-1][1].priority == 42

    def test_update_priority(self):
        job = self.add_job(state=STATE_PENDING)
        _, deferred = self.post(f"/jobs/{job.id}/priority", priority=75)
        assert deferred.wait(0)["job"]["priority"] == 75

    def test_invalid_priority_and_refused_job(self):
        job = self.add_job(state=STATE_PENDING)
        for value in (-1, 1001, True, 3.2, "7", None):
            with pytest.raises(ApiError) as error:
                self.post(f"/jobs/{job.id}/priority", priority=value)
            assert error.value.status == 400
        self.service.refusal = "Only waiting jobs can change priority"
        with pytest.raises(ApiError) as error:
            self.post(f"/jobs/{job.id}/priority", priority=3)
        assert error.value.status == 409

    def test_job_leaving_queue_is_a_conflict(self):
        job = self.add_job(state=STATE_PENDING)
        self.service.answers = {"priority": False}
        _, deferred = self.post(f"/jobs/{job.id}/priority", priority=5)
        with pytest.raises(ApiError) as error:
            deferred.wait(0)
        assert error.value.status == 409

    def test_priority_is_rejected_for_external_schedulers(self):
        self.host.scheduler = "slurm"
        with pytest.raises(ApiError) as error:
            self.post("/jobs", **self.submit_body(priority=3))
        assert error.value.status == 400
        assert not self.service.submitted


def test_client_and_cli_use_numeric_priority_including_zero_override():
    client = JobManagerClient(url="http://127.0.0.1:1", token="test")
    client.request = Mock(return_value={"updated": True})
    assert client.set_priority("job1", 10) == {"updated": True}
    client.request.assert_called_once_with("POST", "jobs/job1/priority", body={"priority": 10})
    args = build_parser().parse_args(
        ["submit", "--host", "myhost", "--command", "true", "--priority", "0"]
    )
    assert _submit_fields(args)["priority"] == 0
