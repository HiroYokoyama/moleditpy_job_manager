"""Failure boundaries discovered by reading submission and monitoring paths."""

import pytest

from job_manager import runner
from job_manager.models import STATE_RUNNING
from job_manager.transport.base import CommandResult, TransportError

from .fakes import FakeTransport, make_host, make_job


def test_failed_completion_read_is_not_reported_as_lost():
    host = make_host()
    transport = FakeTransport(host)
    transport.when("squeue", stdout="")
    transport.default = CommandResult(255, "", "connection reset")
    job = make_job(state=STATE_RUNNING)
    with pytest.raises(TransportError, match="connection reset"):
        runner.poll_host(transport, host, [job])
