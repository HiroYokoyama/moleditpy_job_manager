"""Failure boundaries discovered by reading submission and monitoring paths."""

import pytest

from job_manager import remote_runner, runner
from job_manager.transport.base import CommandResult, TransportError

from .fakes import FakeTransport, make_host, make_job


@pytest.mark.parametrize("operation", [runner.cancel_in_runner, runner.release_in_runner])
@pytest.mark.parametrize("scheduler", ["shell", "windows"])
def test_helper_control_failure_is_reported(operation, scheduler):
    host = make_host(scheduler=scheduler)
    transport = FakeTransport(host)
    transport.default = CommandResult(1, "", "permission denied")
    job = make_job(
        scheduler=scheduler,
        remote_job_id=remote_runner.entry_name(
            1, "job1", ".ps1" if scheduler == "windows" else ".sh"
        ),
    )
    with pytest.raises(TransportError, match="permission denied"):
        operation(transport, host, job)
