"""A waiting Job Server job shows how many jobs the server will start first."""

import tempfile

import pytest

from job_manager.job_database import _STATE_FIELDS
from job_manager.models import Job, queue_position
from job_manager.server_backend import JobServerBackend


class FakeClient:
    def __init__(self, records):
        self.records = records

    def request(self, method, path):
        if path.startswith("/jobs?"):
            return list(self.records.values())
        return self.records[path.rsplit("/", 1)[1]]


def _record(job_id, state, **extra):
    return {"id": job_id, "state": state, "exit_code": None, **extra}


def _poll(records, *jobs):
    backend = object.__new__(JobServerBackend)
    backend.client = FakeClient(records)
    return backend.poll(list(jobs))


def test_poll_takes_the_servers_count_and_forgets_it_once_running():
    waiting = Job(id="a", remote_job_id="s1", state="PENDING")
    started = Job(id="b", remote_job_id="s2", state="PENDING", server_queue_ahead=4)
    _poll(
        {"s1": _record("s1", "PENDING", queue_ahead=3), "s2": _record("s2", "RUNNING")},
        waiting,
        started,
    )
    assert waiting.server_queue_ahead == 3
    assert started.server_queue_ahead == -1


@pytest.mark.parametrize("value", [None, "2", True, 1.5])
def test_an_older_or_odd_server_reply_reads_as_unknown(value):
    job = Job(id="a", remote_job_id="s1", state="PENDING", server_queue_ahead=2)
    record = _record("s1", "PENDING")
    if value is not None:
        record["queue_ahead"] = value
    _poll({"s1": record}, job)
    assert job.server_queue_ahead == -1


def test_wording_and_round_trip():
    assert queue_position(0) == "next in line"
    assert queue_position(5) == "5 ahead"
    job = Job(id="a", server_queue_ahead=2)
    assert Job.from_dict(job.to_dict()).server_queue_ahead == 2
    assert Job.from_dict({"id": "old"}).server_queue_ahead == -1
    # A poller's value, not a user's edit, when two windows save at once.
    assert "server_queue_ahead" in _STATE_FIELDS


def test_poller_carries_the_position_back_without_a_write():
    pytest.importorskip("PySide6.QtWidgets")
    from job_manager.poller import JobPoller
    from job_manager.store import JobStore

    from .fakes import make_host

    store = JobStore(tempfile.mkdtemp(prefix="queue_pos_"))
    host = make_host()
    store.add_host(host)
    poller = JobPoller(store, lambda h: None)
    try:
        store.add_job(Job(id="a", host_id=host.id, remote_job_id="s1", state="PENDING"))
        saved = []
        store.save_jobs = lambda *a, **k: saved.append(1)
        poller._on_poll_finished(host.id, {"a": "PENDING"}, {}, {}, {"a": 2})
        assert store.jobs["a"].server_queue_ahead == 2
        assert saved == []
    finally:
        poller.stop()
