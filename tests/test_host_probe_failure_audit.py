"""Actual service callbacks with synthetic jobs and no network."""

from job_manager.transport.base import CommandResult

from .test_service import ServiceTestCase


class TestAudit(ServiceTestCase):
    def test_failed_host_probe_reports_failure_and_backs_off(self):
        from job_manager.host_sampler import HostSampler

        sampler = HostSampler(self.service)
        self.addCleanup(sampler.deleteLater)
        self.transport.default = CommandResult(255, "", "connection reset")
        errors, samples = [], []
        sampler.sample_failed.connect(lambda *args: errors.append(args))
        sampler.sampled.connect(lambda *args: samples.append(args))
        sampler._sample(self.host)
        assert errors and "connection reset" in errors[0][1]
        assert not samples
        assert not sampler.latest(self.host.id).ok
        assert sampler._skip_ticks[self.host.id] > 0
