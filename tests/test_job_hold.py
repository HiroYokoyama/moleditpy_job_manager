"""Hold display and the headless API preserve dependency gates."""

import pytest

from job_manager.api_core import ApiError, job_payload
from job_manager.models import Job, STATE_HELD, hold_reasons
from .test_api_core import ApiTestCase


def test_held_record_round_trips_and_stays_active_without_run_time():
    job = Job(state=STATE_HELD, submitted_at=100, priority=-20, after_job_id="parent")
    restored = Job.from_dict(job.to_dict())
    assert restored.is_active and not restored.is_terminal
    assert restored.elapsed(200) == 0
    assert restored.waiting(200) == 100
    assert restored.priority == -20 and restored.after_job_id == "parent"


def test_manual_and_after_are_independent_hold_reasons():
    parent = Job(id="parent", name="Previous check", state="UPLOADING")
    child = Job(id="child", state=STATE_HELD, after_job_id=parent.id)
    assert hold_reasons(child, {parent.id: parent}) == ["Manual", "After: Previous check"]
    child.state = "PENDING"
    assert hold_reasons(child, {parent.id: parent}) == ["After: Previous check"]
    parent.state = "DONE"
    assert hold_reasons(child, {parent.id: parent}) == []
    child.state = STATE_HELD
    assert hold_reasons(child, {parent.id: parent}) == ["Manual"]
    child.state = "CANCELLED"
    assert hold_reasons(child, {parent.id: parent}) == []


class TestHoldApi(ApiTestCase):
    def test_hold_and_release_preserve_after_and_priority(self):
        parent = self.add_job(name="parent", state="RUNNING")
        job = self.add_job(state="PENDING", after_job_id=parent.id, priority=-5)
        _, reply = self.post(f"/jobs/{job.id}/hold")
        assert reply.wait(1)["job"]["state"] == STATE_HELD
        held = job_payload(job, self.store)
        assert held["hold_reasons"] == ["Manual", "After: parent"]
        _, reply = self.post(f"/jobs/{job.id}/release")
        data = reply.wait(1)["job"]
        assert data["state"] == "PENDING"
        assert data["display_state"] == STATE_HELD
        assert data["hold_reasons"] == ["After: parent"]
        assert data["after_job_id"] == parent.id and data["priority"] == -5

    def test_no_longer_waiting_is_a_conflict(self):
        job = self.add_job(state="PENDING")
        self.service.answers["hold"] = False
        _, result = self.post(f"/jobs/{job.id}/hold")
        with pytest.raises(ApiError) as caught:
            result.wait(1)
        assert caught.value.status == 409

    def test_refusal_is_a_conflict_and_get_is_not_an_action(self):
        job = self.add_job(state="RUNNING")
        self.service.refusal = "Only waiting jobs can be held"
        with pytest.raises(ApiError) as caught:
            self.post(f"/jobs/{job.id}/hold")
        assert caught.value.status == 409
        with pytest.raises(ApiError) as caught:
            self.get(f"/jobs/{job.id}/hold")
        assert caught.value.status == 405


@pytest.mark.parametrize(
    ("where", "before", "expected"),
    [
        ("held", "PENDING", {"child": "HELD"}),
        ("queue", "HELD", {"child": "PENDING"}),
        ("running", "PENDING", {"child": "RUNNING"}),
        ("held", "HELD", {}),
        ("done", "PENDING", {}),
        ("", "PENDING", {"child": "LOST"}),
    ],
)
def test_poll_recovers_a_job_moved_between_directory_scans(where, before, expected):
    from unittest.mock import patch
    from job_manager.runner import poll_runner
    from job_manager.transport.base import CommandResult
    from .fakes import FakeTransport, make_host

    host = make_host(scheduler="shell", concurrency_mode="runner")
    transport = FakeTransport(host)
    job = Job(id="child", host_id=host.id, state=before, remote_job_id="job_0001_child.sh")
    listing = f"{where} job_0001_child.sh" if where else ""
    with (
        patch.object(
            transport, "run", side_effect=[CommandResult(0, "", ""), CommandResult(0, listing, "")]
        ) as query,
        patch("job_manager.runner._read_sentinels", return_value=["LOST"]),
        patch("job_manager.runner._runner_statuses", return_value={}),
    ):
        assert poll_runner(transport, host, [job]) == expected
    assert query.call_count == 2
