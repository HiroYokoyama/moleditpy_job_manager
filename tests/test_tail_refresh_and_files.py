"""Tests for TextDialog auto-refresh and specific file tailing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from job_manager.models import HostProfile, Job
from job_manager.runner import tail_remote_file
from job_manager.service import JobService
from job_manager.store import JobStore
from job_manager.text_dialog import TextDialog


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def temp_store(tmp_path):
    return JobStore(str(tmp_path))


@pytest.fixture
def service(temp_store):
    return JobService(temp_store)


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
        service.pool.waitForDone(2000)
        qapp.processEvents()
        assert results == ["specific tail contents"]
