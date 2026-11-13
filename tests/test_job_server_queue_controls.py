"""Hold, force run and priority reach a Job Server job, and 0 cores / 0 memory are allowed."""

import pytest

# The Job Server client is optional; a run without its dependency skips these.
pytest.importorskip("cryptography")

from job_manager.job_server_client.client import ClientError  # noqa: E402
from job_manager.models import BACKEND_JOB_SERVER, HostProfile, Job, SubmitPreset
from job_manager.server_backend import JobServerBackend


class FakeClient:
    def __init__(self, status=None):
        self.calls, self.status = [], status

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.status:
            raise ClientError(self.status, {"message": "refused"})
        if path.startswith("/jobs?"):
            return [{"id": "s1", "state": "PENDING", "held": True, "exit_code": None}]
        return {}


def _backend(status=None):
    backend = object.__new__(JobServerBackend)
    backend.client = FakeClient(status)
    return backend


def test_hold_force_and_priority_post_to_the_server():
    backend, job = _backend(), Job(id="a", remote_job_id="s1", state="PENDING")
    assert backend.hold(job, True) and backend.hold(job, False)
    assert backend.force(job) and backend.set_priority(job, 7)
    assert backend.client.calls == [
        ("POST", "/jobs/s1/hold", {"held": True}),
        ("POST", "/jobs/s1/hold", {"held": False}),
        ("POST", "/admin/jobs/s1/force-run", {"force_run": True}),
        ("POST", "/admin/jobs/s1/priority", {"priority": 7}),
    ]


def test_a_job_that_stopped_waiting_is_not_an_error_and_a_refusal_says_why():
    job = Job(id="a", remote_job_id="s1", state="PENDING")
    assert _backend(409).hold(job, True) is False
    with pytest.raises(ValueError, match="administrator"):
        _backend(403).force(job)
    with pytest.raises(ValueError, match="too old"):
        _backend(404).hold(job, True)


def test_a_held_server_job_polls_as_held():
    job = Job(id="a", remote_job_id="s1", state="PENDING")
    assert _backend().poll([job]) == {"a": "HELD"}


def test_the_service_lets_server_jobs_hold_force_and_change_priority():
    from types import SimpleNamespace

    from job_manager.service import JobService

    host = HostProfile(id="h", name="myhost", backend=BACKEND_JOB_SERVER)
    assert host.is_job_server
    service = JobService.__new__(JobService)
    service.store = SimpleNamespace(hosts={"h": host}, jobs={})
    waiting = Job(id="a", name="j", host_id="h", remote_job_id="s1", state="PENDING")
    held = Job(id="b", name="j", host_id="h", remote_job_id="s2", state="HELD")
    assert service.hold_refusal(waiting, True) == ""
    assert service.hold_refusal(held, False) == ""
    assert service.priority_refusal(waiting) == ""
    assert service.force_refusal(waiting) == ""
    assert "release it first" in service.force_refusal(held)


@pytest.mark.parametrize("memory", ["0", "0G", "0.0"])
def test_a_job_server_job_may_reserve_nothing(memory, tmp_path, monkeypatch):
    backend = object.__new__(JobServerBackend)
    sent = {}

    class Client:
        server_id = "server"

        def submit(self, spec, files, key):
            sent.update(spec)
            return {"id": "s1", "owner_id": "u", "state": "PENDING"}

        def request(self, method, path, body=None):
            return {"execution_os": "linux"}

    backend.client = Client()
    backend.host = HostProfile(id="h", name="myhost", backend=BACKEND_JOB_SERVER)
    preset = SubmitPreset(command_template="mycommand", cpus_per_task=0, memory=memory)
    backend.submit(preset, Job(id="a", name="waiter"), [])
    assert sent["resources"]["cpus_per_task"] == 0 and sent["resources"]["memory_mb"] == 0


def test_a_typo_that_reads_as_zero_is_still_refused():
    backend = object.__new__(JobServerBackend)
    backend.host = HostProfile(id="h", name="myhost", backend=BACKEND_JOB_SERVER)
    with pytest.raises(ValueError, match="memory"):
        backend.submit(SubmitPreset(command_template="mycommand", memory="eight"), Job(id="a"), [])
