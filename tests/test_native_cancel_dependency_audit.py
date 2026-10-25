from job_manager.models import STATE_CANCELLED, STATE_PENDING, Job

from .test_service import ServiceTestCase


class TestNativeCancellation(ServiceTestCase):
    def test_cancellation_does_not_claim_to_release_native_scheduler_dependency(self):
        predecessor = self.submit()
        dependent = Job(
            host_id=self.host.id,
            scheduler="slurm",
            state=STATE_PENDING,
            remote_job_id="4243",
            after_job_id=predecessor.id,
        )
        self.store.jobs[dependent.id] = dependent
        self.service.cancel(predecessor)
        assert predecessor.state == STATE_CANCELLED
        assert not dependent.chain_any
        assert self.store.chain_blocker(dependent) is predecessor
