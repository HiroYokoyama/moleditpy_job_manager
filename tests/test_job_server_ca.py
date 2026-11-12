from pathlib import Path
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("cryptography")

FINGERPRINT = "ab" * 32
CERTIFICATE = b"synthetic-public-ca"
URL = "https://192.168.1.20:8767"


@pytest.fixture
def trust(monkeypatch):
    from job_manager.job_server_client import trust

    monkeypatch.setattr(trust, "validate_ca", Mock())
    return trust


def test_ca_file_is_saved_automatically_for_a_connection(tmp_path, monkeypatch, trust):
    from job_manager import server_backend

    client = Mock()
    monkeypatch.setattr("job_manager.job_server_client.client.JobServerClient", client)
    result = server_backend.install_server_ca(
        URL, CERTIFICATE, FINGERPRINT, tmp_path, windows_store=False
    )
    path = result["ca_file"]
    assert result["saved_ca"] == path
    assert Path(path).read_bytes() == CERTIFICATE
    assert Path(path).parent == tmp_path / "identity" / "ca"
    assert Path(path).name == FINGERPRINT + ".pem"
    assert client.call_args.kwargs == {"ca_file": path, "timeout": 5}
    client.return_value.request.assert_called_once_with("GET", "/health", authenticated=False)


def test_windows_trust_uses_default_store_for_subsequent_connections(tmp_path, monkeypatch, trust):
    from job_manager import server_backend

    installed = Mock()
    monkeypatch.setattr(trust, "trust_windows_user_ca", installed)
    client = Mock()
    monkeypatch.setattr("job_manager.job_server_client.client.JobServerClient", client)
    result = server_backend.install_server_ca(
        URL, CERTIFICATE, FINGERPRINT, tmp_path, windows_store=True
    )
    assert result["ca_file"] == ""
    assert Path(result["saved_ca"]).read_bytes() == CERTIFICATE
    installed.assert_called_once_with(CERTIFICATE, FINGERPRINT)
    assert client.call_args.kwargs["ca_file"] is None


def test_refused_ca_is_never_saved_or_installed(tmp_path, monkeypatch, trust):
    from job_manager import server_backend

    trust.validate_ca.side_effect = ValueError("The CA must be limited")
    installed = Mock()
    monkeypatch.setattr(trust, "trust_windows_user_ca", installed)
    with pytest.raises(ValueError, match="CA must"):
        server_backend.install_server_ca(URL, CERTIFICATE, FINGERPRINT, tmp_path)
    installed.assert_not_called()
    assert not (tmp_path / "identity").exists()


def test_fingerprint_shown_is_the_one_from_the_downloaded_ca(monkeypatch):
    from job_manager import server_backend
    from job_manager.job_server_client import trust

    monkeypatch.setattr(trust, "fetch_server_ca", Mock(return_value=CERTIFICATE))
    monkeypatch.setattr(trust, "certificate_fingerprint", Mock(return_value="AB" * 32))
    assert server_backend.fetch_ca_for_confirmation(URL) == (CERTIFICATE, "AB" * 32)
    trust.fetch_server_ca.assert_called_once_with(URL)


@pytest.mark.parametrize("confirmed", [True, False])
def test_ca_is_installed_only_after_the_fingerprint_is_confirmed(confirmed, tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from job_manager import server_backend

    def run_now(_pool, work, on_success=None, on_error=None, **_):
        on_success(work())

    field = Mock()
    with (
        patch.object(server_backend, "require_client"),
        patch.object(
            server_backend, "fetch_ca_for_confirmation", return_value=(CERTIFICATE, FINGERPRINT)
        ),
        patch.object(server_backend, "confirm_ca_fingerprint", return_value=confirmed) as confirm,
        patch.object(
            server_backend,
            "install_server_ca",
            return_value={"ca_file": "saved.pem", "saved_ca": "saved.pem"},
        ) as install,
        patch("job_manager.tasks.run_async", run_now),
        patch("job_manager.store.default_data_dir", return_value=str(tmp_path)),
        patch("PySide6.QtWidgets.QMessageBox"),
    ):
        server_backend.show_ca_registration(None, URL, field)
    assert confirm.call_args.args[1] == FINGERPRINT
    if confirmed:
        install.assert_called_once_with(URL, CERTIFICATE, FINGERPRINT, str(tmp_path))
        field.setText.assert_called_once_with("saved.pem")
    else:
        install.assert_not_called()
        field.setText.assert_not_called()


def test_windows_trust_failure_names_the_saved_ca(tmp_path, monkeypatch, trust):
    from job_manager import server_backend

    monkeypatch.setattr(trust, "trust_windows_user_ca", Mock(side_effect=OSError("denied")))
    with pytest.raises(RuntimeError, match="Windows trust registration failed: denied") as info:
        server_backend.install_server_ca(
            URL, CERTIFICATE, FINGERPRINT, tmp_path, windows_store=True
        )
    assert FINGERPRINT + ".pem" in str(info.value)


@pytest.mark.parametrize("answer, confirmed", [("Yes", True), ("No", False)])
def test_the_fingerprint_box_defaults_to_no_and_shows_the_fingerprint(answer, confirmed):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from job_manager import server_backend

    QApplication.instance() or QApplication([])
    shown = {}

    def exec_(box):
        shown["text"] = box.text()
        shown["default"] = box.defaultButton() == box.button(QMessageBox.StandardButton.No)
        return getattr(QMessageBox.StandardButton, answer)

    with patch.object(QMessageBox, "exec", exec_):
        result = server_backend.confirm_ca_fingerprint(None, FINGERPRINT, "Saved for this host.")
    assert result is confirmed
    assert FINGERPRINT in shown["text"] and "Saved for this host." in shown["text"]
    assert shown["default"]


def test_an_invalid_address_is_refused_before_anything_is_fetched():
    pytest.importorskip("PySide6.QtWidgets")
    from job_manager import server_backend

    with (
        patch.object(server_backend, "require_client"),
        patch.object(server_backend, "fetch_ca_for_confirmation") as fetch,
        patch("PySide6.QtWidgets.QMessageBox.warning") as warned,
    ):
        server_backend.show_ca_registration(None, "ftp://example", Mock())
    warned.assert_called_once()
    fetch.assert_not_called()


def test_a_failed_fetch_is_reported():
    pytest.importorskip("PySide6.QtWidgets")
    from job_manager import server_backend

    def run_now(_pool, work, on_success=None, on_error=None, **_):
        on_error("connection refused")

    field = Mock()
    with (
        patch.object(server_backend, "require_client"),
        patch("job_manager.tasks.run_async", run_now),
        patch("PySide6.QtWidgets.QMessageBox.warning") as warned,
    ):
        server_backend.show_ca_registration(None, URL, field)
    assert "connection refused" in warned.call_args.args[2]
    field.setText.assert_not_called()


def test_backend_folder_calls_go_through_the_server_files_helpers():
    pytest.importorskip("PySide6.QtWidgets")
    from job_manager.models import HostProfile
    from job_manager.server_backend import JobServerBackend

    host = HostProfile(backend="job_server", server_url=URL)
    client = Mock()
    with (
        patch("job_manager.server_backend.require_client"),
        patch("job_manager.job_server_client.client.JobServerClient", return_value=client),
    ):
        backend = JobServerBackend(host)
    with (
        patch("job_manager.server_files.list_path", return_value=["a"]) as listed,
        patch("job_manager.server_files.stat_path", return_value={"size": 1}) as stated,
        patch("job_manager.server_files.download_paths", return_value=["x"]) as downloaded,
    ):
        assert backend.list_path("job/out", 2) == ["a"]
        assert backend.stat_path("job/out/a", digest=False) == {"size": 1}
        assert backend.download_paths(["job/out"], "/tmp/d", overwrite=True) == ["x"]
    listed.assert_called_once_with(client, "job/out", 2)
    stated.assert_called_once_with(client, "job/out/a", False)
    downloaded.assert_called_once_with(client, ["job/out"], "/tmp/d", True)
