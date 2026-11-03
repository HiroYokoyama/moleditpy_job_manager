"""Server budget failures stay in Submit without creating partial jobs."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from job_manager.models import HostProfile, SubmitPreset  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from job_manager.submit_dialog import SubmitDialog  # noqa: E402


@pytest.fixture
def submission(tmp_path, monkeypatch):
    service = JobService(JobStore(str(tmp_path)))
    host = HostProfile(
        name="example", backend="job_server", server_url="https://compute.example.org"
    )
    service.store.add_host(host)
    service.submit = Mock()
    queued = []
    monkeypatch.setattr(
        "job_manager.tasks.run_async",
        lambda pool, work, **callbacks: queued.append((work, callbacks)),
    )
    backend = SimpleNamespace(
        client=SimpleNamespace(request=Mock(return_value={"cpus": 2, "memory_mb": 4096}))
    )
    monkeypatch.setattr("job_manager.server_backend.backend_for", lambda host: backend)
    monkeypatch.setattr("job_manager.submit_dialog.ensure_password", lambda *args: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog = SubmitDialog(service)
    dialog.txt_command.setText("echo synthetic")
    dialog.chk_scan_resources.setChecked(False)
    dialog.show()
    dialog.accept = Mock()
    yield dialog, service, host, queued, backend
    dialog.close()
    service.shutdown()


def test_server_over_budget_preserves_inputs_and_allows_adjusted_resubmission(submission):
    dialog, service, _, queued, backend = submission
    dialog.spin_cpus.setValue(8)
    dialog._submit()
    assert not dialog.btn_submit.isEnabled()
    service.submit.assert_not_called()
    work, callbacks = queued.pop()
    callbacks["on_success"](work())
    assert "8" in dialog.lbl_submit_error.text()
    assert "budget" in dialog.lbl_submit_error.text()
    assert dialog.spin_cpus.value() == 8
    assert dialog.txt_command.text() == "echo synthetic"
    dialog.accept.assert_not_called()
    assert not service.store.jobs
    dialog.spin_cpus.setValue(1)
    dialog.txt_memory.setText("512M")
    dialog._submit()
    work, callbacks = queued.pop()
    callbacks["on_success"](work())
    service.submit.assert_called_once()
    dialog.accept.assert_called_once()
    assert backend.client.request.call_count == 2


def test_server_probe_error_and_host_change_do_not_submit(submission):
    dialog, service, host, queued, _ = submission
    dialog._submit()
    _, callbacks = queued.pop()
    callbacks["on_error"]("synthetic connection error")
    assert dialog.btn_submit.isEnabled()
    assert "synthetic connection error" in dialog.lbl_submit_error.text()
    dialog._submit()
    work, callbacks = queued.pop()
    host.server_url = "https://other.example.org"
    callbacks["on_success"](work())
    assert "host changed" in dialog.lbl_submit_error.text()
    service.submit.assert_not_called()
    dialog.accept.assert_not_called()


def test_server_budget_uses_totals_instead_of_current_free_capacity():
    resources = {"cpus": 2, "memory_mb": 4096, "free_cpus": 0, "free_memory_mb": 0}
    assert (
        SubmitDialog._server_resource_overrun(resources, SubmitPreset(cpus_per_task=2, memory="4G"))
        == ""
    )
    assert "Memory" in SubmitDialog._server_resource_overrun(resources, SubmitPreset(memory="8G"))
    with pytest.raises(ValueError):
        SubmitDialog._server_resource_overrun({}, SubmitPreset())


def test_batch_overrun_is_detected_before_any_job_is_submitted(submission, tmp_path, monkeypatch):
    dialog, service, host, _, _ = submission
    paths = [str(tmp_path / "a.inp"), str(tmp_path / "b.inp")]
    dialog.chk_scan_resources.setChecked(True)
    monkeypatch.setattr(
        "job_manager.submit_dialog.input_scan.scan",
        lambda path: SimpleNamespace(cores=8 if path.endswith("b.inp") else 1, memory_mb=0),
    )
    with pytest.raises(ValueError, match="b.inp.*CPU"):
        dialog._submit_batch(
            host,
            SubmitPreset(command_template="echo synthetic", memory="1G"),
            paths,
            server_resources={"cpus": 2, "memory_mb": 4096},
        )
    service.submit.assert_not_called()
