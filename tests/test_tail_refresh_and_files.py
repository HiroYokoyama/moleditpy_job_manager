"""Tests for TextDialog auto-refresh and specific file tailing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from job_manager.models import HostProfile, Job
from job_manager.runner import tail_remote_file
from job_manager.service import JobService
from job_manager.store import JobStore
from job_manager.text_dialog import TextDialog

from .test_poller import SyncPool
from .test_tasks import HeldPool


@pytest.mark.parametrize("fails", [False, True])
def test_tail_answer_does_not_outlive_its_text_window(service, temp_store, qapp, fails):
    import shiboken6
    from job_manager.jobs_dialog import JobsDialog

    host = HostProfile(id="h1", name="Host1", hostname="myhost", username="user")
    temp_store.add_host(host)
    job = Job(id="j1", name="job1", host_id="h1", remote_dir="/jobs/example")
    service.pool = HeldPool()
    dialog = TextDialog("Tail", auto_refresh=False)
    seen = []
    dialog.set_text = seen.append
    monitor = SimpleNamespace(service=service)
    transport = MagicMock()
    transport.run.return_value.stdout = "finished"
    if fails:
        transport.run.side_effect = OSError("host unavailable")
    with patch.object(service, "transport_for", return_value=transport):
        JobsDialog._refresh_tail_file(monitor, job, "output.log", dialog)
        shiboken6.delete(dialog)
        service.pool.run()
    assert seen == []
    service.pool = SyncPool()


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def temp_store(tmp_path):
    return JobStore(str(tmp_path))


@pytest.fixture
def service(temp_store):
    svc = JobService(temp_store)
    svc.pool = SyncPool()
    svc.poller.pool = SyncPool()
    yield svc
    svc.shutdown()


def test_text_dialog_auto_refresh(qapp):
    refresh_called = []

    def on_refresh():
        refresh_called.append(True)

    dlg = TextDialog("Test Tail", "Initial Text", on_refresh=on_refresh, auto_interval=2)
    assert hasattr(dlg, "chk_auto_refresh")
    assert hasattr(dlg, "spin_interval")
    assert dlg.spin_interval.value() == 2

    # Initially checked by default
    assert dlg.chk_auto_refresh.isChecked()
    assert dlg._timer.isActive()

    # Toggle unchecked -> timer stops
    dlg.chk_auto_refresh.setChecked(False)
    assert not dlg._timer.isActive()

    # Toggle checked again -> timer starts and refresh is called
    dlg.chk_auto_refresh.setChecked(True)
    assert dlg._timer.isActive()

    # Changing interval updates timer interval
    dlg.spin_interval.setValue(10)
    assert dlg._timer.interval() == 10000

    # Closing stops timer
    dlg.close()
    dlg.deleteLater()
    assert not dlg._timer.isActive()


def test_tail_remote_file_runner():
    mock_transport = MagicMock()
    mock_transport.host.scheduler = "none"
    mock_transport.run.return_value.stdout = "line 1\nline 2"
    mock_transport.run.return_value.stderr = ""

    job = Job(id="j1", name="job1", host_id="h1", remote_dir="/remote/path", log_file="job.log")
    out = tail_remote_file(mock_transport, job, "output.out", lines=50)
    assert "line 1" in out
    assert mock_transport.run.called


def test_service_tail_file(service, temp_store, qapp):
    host = HostProfile(id="h1", name="Host1", hostname="cluster.edu", username="user")
    temp_store.add_host(host)
    job = Job(id="j1", name="job1", host_id="h1", remote_dir="/remote/path", log_file="job.log")

    mock_transport = MagicMock()
    mock_transport.host = host
    mock_transport.run.return_value.stdout = "specific tail contents"
    mock_transport.run.return_value.stderr = ""

    with patch.object(service, "transport_for", return_value=mock_transport):
        results = []
        service.tail_file(job, "calc.out", lines=100, on_done=lambda txt: results.append(txt))
        # No processEvents(). The service fixture swaps in SyncPool, whose
        # start() calls run_sync() inline, so on_done has already fired by the
        # time tail_file returns -- pumping the queue added nothing to this
        # assertion and only drained whatever the rest of the suite had left
        # in it. That is a global side effect in a test that is about one
        # function, and it crashed the Windows job with an access violation,
        # intermittently, because xdist varies what ran before it.
        assert results == ["specific tail contents"]
