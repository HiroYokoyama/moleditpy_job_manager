"""Exercise the Python-to-helper Hold protocol against both real local shells."""

import os
import unittest
from unittest.mock import patch

import pytest
from job_manager import remote_runner
from job_manager.models import SCHEDULER_WINDOWS
from job_manager.runner import poll_runner, set_runner_hold
from job_manager.transport.base import CommandResult, TransportError
from .fakes import FakeTransport, make_host
from .test_runner_end_to_end import BASH, ON_WINDOWS, EndToEndCase


class HoldBridgeBehavior:
    def test_host_hold_poll_release_and_late_request_after_completion(self):
        self.host.runner_cores = 2
        self.host.runner_memory_mb = 512
        flavour = remote_runner.flavour_for(self.host)
        transport = self.transport()
        transport.run(flavour.prepare_command(self.directory)).check("prepare test helper")
        transport.run(flavour.pause_command(self.directory, True)).check("pause test helper")
        self.addCleanup(lambda: transport.run(flavour.pause_command(self.directory, False)))
        job = self.submit()
        assert set_runner_hold(transport, self.host, job, True)
        assert poll_runner(transport, self.host, [job]) == {job.id: "HELD"}
        job.touch("HELD")
        assert poll_runner(transport, self.host, [job]) == {}
        assert not os.path.exists(self.marker())
        assert set_runner_hold(transport, self.host, job, False)
        assert poll_runner(transport, self.host, [job]) == {job.id: "PENDING"}
        transport.run(flavour.pause_command(self.directory, False)).check("resume test helper")
        self.wait_for(lambda: os.path.exists(self.marker()), timeout=90, what="released payload")
        self.wait_for(
            lambda: poll_runner(transport, self.host, [job]).get(job.id) == "DONE",
            timeout=90,
            what="completion after release",
        )
        assert not set_runner_hold(transport, self.host, job, True)


@unittest.skipUnless(BASH, "needs Bash")
class TestBashHoldBridge(HoldBridgeBehavior, EndToEndCase):
    pass


@unittest.skipUnless(ON_WINDOWS, "needs Windows PowerShell")
class TestPowerShellHoldBridge(HoldBridgeBehavior, EndToEndCase):
    scheduler = SCHEDULER_WINDOWS

    def command_that_touches(self, path):
        return f"New-Item -ItemType File -Path '{path}' | Out-Null"


def test_hold_protocol_does_not_treat_an_unknown_reply_as_success():
    from job_manager.models import Job

    host = make_host(scheduler="shell", concurrency_mode="runner")
    job = Job(id="abc", host_id=host.id, state="PENDING", remote_job_id="job_0001_abc.sh")
    transport = FakeTransport(host)
    transport.default = CommandResult(0, "truncated reply", "")
    with (
        patch("job_manager.runner._prepare_runner", return_value="helper.sh"),
        pytest.raises(TransportError, match="did not confirm"),
    ):
        set_runner_hold(transport, host, job, True)
