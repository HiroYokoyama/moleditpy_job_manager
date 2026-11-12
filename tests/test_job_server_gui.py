"""Server routing through ordinary Job Manager dialogs and service operations."""

from unittest.mock import Mock, patch

import pytest

pytest.importorskip("PySide6.QtWidgets")
from job_manager.host_profile_form import (  # noqa: E402
    BACKEND_LOCAL,
    BACKEND_OPENSSH,
    BACKEND_PARAMIKO,
    MODE_LANES,
    MODE_RUNNER,
)
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
        assert not form.chk_server_lan_http.isChecked()
        assert not form.btn_server_trust.isHidden()
        form.chk_server_lan_http.setChecked(True)
        assert not form.txt_server_key.isEnabled()
        assert form.txt_server_key.isReadOnly()
        assert not form.txt_server_id.isEnabled()
        assert form.btn_server_sign_in.isEnabled()
        assert "Sign in" in form.btn_server_sign_in.text()
        assert not dialog.txt_hostname.isEnabled()
        assert not dialog.cmb_concurrency.isEnabled()
        assert dialog.queue_box.isHidden()
        form.txt_server_url.setText(host.server_url)
        form.txt_server_key.setText("example-device.key")
        form._server_enrolled({"server_id": "example-server", "execution_os": "windows"})
        assert dialog.cmb_scheduler.currentData() == "windows"
        assert form._collect(HostProfile()).server_id == "example-server"
        assert form._collect(HostProfile()).server_allow_lan_http
        with patch("job_manager.server_backend.show_enrollment") as enroll:
            form._server_sign_in()
            assert enroll.call_args.kwargs == {"allow_lan_http": True}
        assert not form.btn_server_sign_in_again.isHidden()  # A credential exists.
        with patch("job_manager.server_backend.show_enrollment") as enroll:
            form.btn_server_sign_in_again.click()
            assert enroll.call_args.kwargs == {"allow_lan_http": True, "again": True}
        form.txt_server_key.clear()
        assert form.btn_server_sign_in_again.isHidden()
        dialog.cmb_backend.setCurrentIndex(dialog.cmb_backend.findData("openssh"))
        assert form.txt_server_url.isHidden()
        assert form.chk_server_lan_http.isHidden()
        assert form.btn_server_trust.isHidden()
        assert dialog.txt_hostname.isEnabled()
        dialog.reject = lambda: None
        dialog.hide()
        dialog.deleteLater()


def test_server_credentials_use_one_dialog_and_clear_password(server_service, monkeypatch):
    from PySide6.QtWidgets import QDialog, QLineEdit
    from job_manager.server_backend import server_credentials

    calls, passwords = [], []

    def enter(dialog):
        calls.append(dialog)
        username = dialog.findChild(QLineEdit, "username")
        password = dialog.findChild(QLineEdit, "password")
        assert username is not None and password is not None
        username.setText(" fixture-user ")
        password.setText("synthetic-password")
        assert password.echoMode() == QLineEdit.EchoMode.Password
        passwords.append(password)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", enter)
    assert server_credentials(None) == ("fixture-user", "synthetic-password")
    assert len(calls) == 1
    assert passwords[0].text() == ""


def test_cancelled_server_login_returns_no_credentials(server_service, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from job_manager.server_backend import server_credentials

    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    assert server_credentials(None) is None


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


def test_backend_comes_before_the_rows_it_decides(server_service):
    service, _ = server_service
    with patch("job_manager.server_backend.client_available", return_value=True):
        dialog = HostsDialog(service)
        form = dialog.profile_form
        row = lambda widget: form.connection_form.getWidgetPosition(widget)[0]  # noqa: E731
        assert row(form.txt_name) < row(form.cmb_backend) < row(form.lbl_backend_hint)
        assert row(form.lbl_backend_hint) < row(form.txt_hostname)
        assert row(form.txt_server_url) < row(form.btn_server_trust)
        assert row(form.btn_server_trust) < row(form.btn_server_sign_in)


def test_rows_a_backend_does_not_use_are_hidden(server_service):
    service, _ = server_service
    with patch("job_manager.server_backend.client_available", return_value=True):
        dialog = HostsDialog(service)
        form = dialog.profile_form
        hidden = lambda widget: widget.isHidden()  # noqa: E731
        choose = lambda data: dialog.cmb_backend.setCurrentIndex(  # noqa: E731
            dialog.cmb_backend.findData(data)
        )

        choose("job_server")
        for widget in (
            form.txt_hostname,
            form.key_row,
            form.txt_remote_root,
            form.cmb_scheduler,
            form.spin_max_concurrent,
            form.cmb_concurrency,
            form.spin_runner_cores,
            form.txt_server_key,
            form.txt_server_id,
            form.chk_load_profile,
            form.spin_command_timeout,
            form.txt_submit_options,
            form.txt_options,
            form.chk_monitor_usage,
            form.chk_ask_password,
        ):
            assert hidden(widget)
        assert form.connection_form.labelForField(form.txt_hostname).isHidden()
        assert not hidden(form.txt_server_url)
        assert not hidden(form.adv_box) and not hidden(form.txt_login)
        assert form.txt_login.isEnabled()
        form.txt_server_id.setText("server-identity")
        assert not hidden(form.txt_server_id)

        choose(BACKEND_LOCAL)
        assert hidden(form.txt_hostname) and hidden(form.equal_path_row)
        assert hidden(form.txt_server_url) and hidden(form.txt_server_id)
        assert hidden(form.txt_options)
        assert not hidden(form.chk_load_profile) and not hidden(form.spin_command_timeout)
        assert not hidden(form.txt_remote_root) and not hidden(form.cmb_scheduler)

        choose(BACKEND_PARAMIKO)
        assert not hidden(form.txt_hostname) and not hidden(form.equal_path_row)
        assert not hidden(form.chk_ask_password) and not hidden(form.txt_options)

        choose(BACKEND_OPENSSH)
        assert hidden(form.chk_ask_password)


def test_queue_rows_follow_the_scheduler_and_queueing_mode(server_service):
    service, _ = server_service
    with patch("job_manager.server_backend.client_available", return_value=True):
        dialog = HostsDialog(service)
        form = dialog.profile_form
        dialog.cmb_backend.setCurrentIndex(dialog.cmb_backend.findData(BACKEND_OPENSSH))
        dialog.cmb_scheduler.setCurrentIndex(dialog.cmb_scheduler.findData("shell"))
        dialog.cmb_concurrency.setCurrentIndex(dialog.cmb_concurrency.findData(MODE_RUNNER))
        assert not form.spin_runner_cores.isHidden() and not form.cmb_concurrency.isHidden()
        dialog.cmb_concurrency.setCurrentIndex(dialog.cmb_concurrency.findData(MODE_LANES))
        assert form.spin_runner_cores.isHidden() and not form.cmb_concurrency.isHidden()
        dialog.cmb_scheduler.setCurrentIndex(dialog.cmb_scheduler.findData("slurm"))
        assert form.cmb_concurrency.isHidden() and form.spin_runner_cores.isHidden()


def test_login_commands_are_saved_for_a_server_host(server_service):
    service, host = server_service
    with patch("job_manager.server_backend.client_available", return_value=True):
        dialog = HostsDialog(service)
        dialog.cmb_backend.setCurrentIndex(dialog.cmb_backend.findData("job_server"))
        dialog.txt_login.setPlainText("conda activate orca\n\nmodule load orca\n")
        assert dialog._collect(host).login_commands == ["conda activate orca", "module load orca"]


def test_login_commands_start_each_server_job(server_service):
    from job_manager.server_backend import JobServerBackend

    _, host = server_service
    host.login_commands = ["conda activate orca", "module load orca"]
    preset = SubmitPreset(command_template="orca {input}", pre_commands=["export A=1"])
    job = Mock(
        id="example-job",
        remote_dir_provided=False,
        remote_input="",
        start_after=None,
        force_run=False,
        auto_download=True,
        chain_any=False,
    )
    job.name = "example"
    client = Mock()
    client.submit.return_value = {"id": "server-job", "owner_id": "owner", "state": "PENDING"}
    client.request.return_value = {"execution_os": "linux"}
    with patch("job_manager.job_server_client.client.JobServerClient", return_value=client):
        JobServerBackend(host).submit(preset, job, [])
    spec = client.submit.call_args.args[0]
    assert spec["command"].splitlines() == [
        "conda activate orca",
        "module load orca",
        "export A=1",
        "orca {input}",
    ]
