"""Queue writes use a worker and persist limits only after host success."""

from unittest.mock import Mock
import pytest

pytest.importorskip("PySide6.QtCore")
from job_manager.service import JobService
from job_manager.models import HostProfile
from job_manager.runner import apply_queue_limits
from job_manager.transport.base import CommandResult, TransportError


def test_queue_limits_failure_is_reported():
    transport = Mock()
    transport.run.return_value = CommandResult(rc=1, stdout="", stderr="permission denied")
    with pytest.raises(TransportError, match="permission denied"):
        apply_queue_limits(transport, HostProfile())


@pytest.mark.parametrize("failure", [False, True])
def test_service_closes_transport_and_persists_after_success(monkeypatch, failure):
    host = HostProfile(max_concurrent=2)
    transport = Mock()
    store = Mock(hosts={host.id: host})
    service = Mock(store=store, _queue_writes=set())
    service.transport_for.return_value = transport
    captured = []

    def apply(_transport, snapshot):
        assert snapshot is not host and snapshot.max_concurrent == 4
        assert host.max_concurrent == 2
        assert host.id in service._queue_writes
        if failure:
            raise RuntimeError("host unavailable")

    def run(_pool, work, on_success, on_error, owner):
        try:
            value = work()
        except RuntimeError as exc:
            on_error(str(exc))
        else:
            on_success(value)

    monkeypatch.setattr("job_manager.runner.apply_queue_limits", apply)
    monkeypatch.setattr("job_manager.service.run_async", run)
    JobService.configure_queue(
        service, host, {"max_concurrent": 4}, captured.append, captured.append
    )
    transport.close.assert_called_once()
    assert host.id not in service._queue_writes
    if failure:
        assert host.max_concurrent == 2 and captured == ["host unavailable"]
        store.save_settings.assert_not_called()
    else:
        assert host.max_concurrent == 4
        assert captured[0]["limits"]["max_concurrent"] == 4
        store.save_settings.assert_called_once()


def test_service_pause_does_not_apply_limits_or_save_profile(monkeypatch):
    host = HostProfile()
    service = Mock(_queue_writes=set())
    service.store.hosts = {host.id: host}
    transport = service.transport_for.return_value
    pause = Mock(return_value=True)
    limits = Mock()
    monkeypatch.setattr("job_manager.runner.set_queue_paused", pause)
    monkeypatch.setattr("job_manager.runner.apply_queue_limits", limits)
    monkeypatch.setattr(
        "job_manager.service.run_async",
        lambda pool, work, on_success, on_error, owner: on_success(work()),
    )
    done = Mock()
    JobService.configure_queue(service, host, {"paused": True}, done, Mock())
    assert done.call_args.args[0]["paused"] is True
    pause.assert_called_once()
    limits.assert_not_called()
    service.store.save_settings.assert_not_called()
    transport.close.assert_called_once()
    assert not service._queue_writes
