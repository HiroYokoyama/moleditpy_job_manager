from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("cryptography")


def test_ca_file_is_saved_automatically_for_a_connection(tmp_path, monkeypatch):
    from job_manager import server_backend
    from job_manager.job_server_client import trust

    certificate = b"synthetic-public-ca"
    monkeypatch.setattr(trust, "fetch_server_ca", lambda *args: certificate)
    client = Mock()
    monkeypatch.setattr("job_manager.job_server_client.client.JobServerClient", client)
    fingerprint = "ab" * 32
    result = server_backend.install_server_ca(
        "https://192.168.1.20:8767", fingerprint, tmp_path, windows_store=False
    )
    path = result["ca_file"]
    assert result["saved_ca"] == path
    assert Path(path).read_bytes() == certificate
    assert Path(path).parent == tmp_path / "identity" / "ca"
    assert Path(path).name == fingerprint + ".pem"
    assert client.call_args.kwargs == {"ca_file": path, "timeout": 5}
    client.return_value.request.assert_called_once_with("GET", "/health", authenticated=False)


def test_windows_trust_uses_default_store_for_subsequent_connections(tmp_path, monkeypatch):
    from job_manager import server_backend
    from job_manager.job_server_client import trust

    certificate = b"synthetic-public-ca"
    monkeypatch.setattr(trust, "fetch_server_ca", lambda *args: certificate)
    installed = Mock()
    monkeypatch.setattr(trust, "trust_windows_user_ca", installed)
    client = Mock()
    monkeypatch.setattr("job_manager.job_server_client.client.JobServerClient", client)
    fingerprint = "ab" * 32
    result = server_backend.install_server_ca(
        "https://192.168.1.20:8767", fingerprint, tmp_path, windows_store=True
    )
    assert result["ca_file"] == ""
    assert Path(result["saved_ca"]).read_bytes() == certificate
    installed.assert_called_once_with(certificate, fingerprint)
    assert client.call_args.kwargs["ca_file"] is None


def test_unverified_ca_is_never_saved_or_installed(tmp_path, monkeypatch):
    from job_manager import server_backend
    from job_manager.job_server_client import trust

    def mismatch(*args):
        raise ValueError("CA fingerprint differs")

    monkeypatch.setattr(trust, "fetch_server_ca", mismatch)
    installed = Mock()
    monkeypatch.setattr(trust, "trust_windows_user_ca", installed)
    with pytest.raises(ValueError, match="fingerprint differs"):
        server_backend.install_server_ca("https://192.168.1.20:8767", "ab" * 32, tmp_path)
    installed.assert_not_called()
    assert not (tmp_path / "identity").exists()
