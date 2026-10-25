"""Actual service callbacks with synthetic jobs and no network."""

from unittest.mock import patch

from job_manager.models import STATE_FAILED, Job
from job_manager import remote_runner

from .test_service import ServiceTestCase


class TestAudit(ServiceTestCase):
    def test_submission_does_not_drop_failed_predecessor(self):
        predecessor = Job(host_id=self.host.id, state=STATE_FAILED)
        self.store.jobs[predecessor.id] = predecessor
        job = self.submit(after_job=predecessor)
        assert job.state == STATE_FAILED
        assert not job.remote_job_id
        assert not self.transport.uploads

    def test_helper_submission_waits_for_predecessor_before_connecting(self):
        self.host.scheduler = "shell"
        self.host.concurrency_mode = "runner"
        predecessor = Job(host_id=self.host.id, state="UPLOADING")
        self.store.jobs[predecessor.id] = predecessor
        events = []

        def resolve(*args, **kwargs):
            events.append("wait")
            predecessor.remote_job_id = remote_runner.entry_name(1, predecessor.id)
            return predecessor.remote_job_id

        def connect(host):
            events.append("connect")
            return self.transport

        with (
            patch.object(self.service, "_chain_pid", side_effect=resolve),
            patch.object(self.service, "transport_for", side_effect=connect),
            patch(
                "job_manager.submission_workflow.submit_to_runner",
                side_effect=lambda *a, **kw: a[3],
            ),
        ):
            self.submit(after_job=predecessor)
        assert events[:2] == ["wait", "connect"]

    def test_timed_out_predecessor_is_not_dropped(self):
        predecessor = Job(host_id=self.host.id, state="UPLOADING")
        with self.assertRaises(TimeoutError):
            self.service._chain_pid(predecessor, timeout=0)
