"""Server routing through ordinary Job Manager dialogs and service operations."""

from unittest.mock import Mock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from job_manager.hosts_dialog import HostsDialog  # noqa: E402
from job_manager.models import HostProfile, SubmitPreset  # noqa: E402
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from .test_poller import SyncPool, _PollTask  # noqa: E402


@pytest.fixture
def server_service(tmp_path):
    store = JobStore(str(tmp_path))
    host = HostProfile(
        name="example",
        backend="job_server",
        server_url="https://compute.example.org",
        scheduler="shell",
        load_profile=False,
    )
    store.add_host(host)
    service = JobService(store)
    service.pool = service.poller.pool = SyncPool()
    yield service, host
    service.shutdown()


def test_server_form_hides_ssh_queue_and_restores_normal_hosts(server_service):
    service, host = server_service
    with patch("job_manager.server_backend.client_available", return_value=True):
        dialog = HostsDialog(service)
        dialog.cmb_backend.setCurrentIndex(dialog.cmb_backend.findData("job_server"))
        form = dialog.profile_form
        assert not form.txt_server_url.isHidden()
        assert not dialog.txt_hostname.isEnabled()
        assert not dialog.cmb_concurrency.isEnabled()
        assert dialog.queue_box.isHidden()
        form.txt_server_url.setText(host.server_url)
        form.txt_server_key.setText("example-device.key")
        form._server_enrolled({"server_id": "example-server", "execution_os": "windows"})
        assert dialog.cmb_scheduler.currentData() == "windows"
        assert form._collect(HostProfile()).server_id == "example-server"
        dialog.cmb_backend.setCurrentIndex(dialog.cmb_backend.findData("openssh"))
        assert form.txt_server_url.isHidden()
        assert dialog.txt_hostname.isEnabled()
        dialog.reject = lambda: None
        dialog.hide()
        dialog.deleteLater()


def test_submit_poll_cancel_and_result_routing_never_create_shell_transport(
    server_service, tmp_path
):
    service, host = server_service
    backend = Mock()
    identifier = "example-server-job"

    def submit(preset, job, files, after_id):
        job.remote_job_id = job.server_job_id = identifier
        job.server_id = "example-server"
        job.touch("PENDING")
        return job

    backend.submit.side_effect = submit
    backend.cancel.return_value = {"state": "RUNNING"}
    backend.tail.return_value = "synthetic log"
    backend.files.return_value = [{"path": "result.out", "size": 1}]
    service.transport_for = Mock(side_effect=AssertionError("shell transport must not be created"))
    with patch("job_manager.server_backend.backend_for", return_value=backend):
        job = service.submit(host, SubmitPreset(command_template="echo hello"), "synthetic", [])
        assert job.state == "PENDING"
        assert job.server_job_id == identifier
        backend.poll.return_value = {job.id: "RUNNING"}
        task = _PollTask(host, [job], service.transport_for)
        updates = []
        task.signals.finished.connect(lambda host_id, states, codes, errors: updates.append(states))
        task.run_sync()
        assert updates == [{job.id: "RUNNING"}]
        service.cancel(job)
        assert job.state == "RUNNING"
        results = []
        service.list_remote_results(
            job, on_ok=results.extend, on_error=lambda message: pytest.fail(message)
        )
        assert results == ["result.out"]
        backend.fetch.return_value = [str(tmp_path / "result.out")]
        assert service.download(job, into=str(tmp_path))
        backend.fetch.assert_called_once()
        service.transport_for.assert_not_called()
