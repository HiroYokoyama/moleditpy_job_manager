"""Actual service callbacks with synthetic jobs and no network."""

from job_manager.models import STATE_RUNNING, Job
from job_manager import remote_runner
from job_manager.transport.base import CommandResult

from .test_service import ServiceTestCase


class TestAudit(ServiceTestCase):
    def test_failed_cancellation_keeps_dependency_policy(self):
        job = self.submit()
        self.host.scheduler = job.scheduler = "shell"
        self.host.concurrency_mode = "runner"
        job.remote_job_id = remote_runner.entry_name(1, job.id)
        job.touch(STATE_RUNNING)
        dependent = Job(host_id=self.host.id, after_job_id=job.id, state="PENDING")
        self.store.jobs[dependent.id] = dependent
        self.transport.clear_rules()
        self.transport.default = CommandResult(1, "", "permission denied")
        errors = []
        self.service.error.connect(errors.append)
        self.service.cancel(job)
        assert job.state == STATE_RUNNING
        assert not dependent.chain_any
        assert errors and "permission denied" in errors[0]
