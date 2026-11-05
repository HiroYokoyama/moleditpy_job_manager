from unittest.mock import Mock, patch

import pytest

from job_manager.models import HostProfile
from job_manager.server_backend import backend_for, validate_server_url


@pytest.mark.parametrize("address", ["192.168.1.2", "10.0.0.2", "172.31.0.2", "[fd12::2]"])
def test_private_http_requires_explicit_per_host_opt_in(address):
    url = f"http://{address}:8080"
    with pytest.raises(ValueError):
        validate_server_url(url)
    assert validate_server_url(url, True) == url
    host = HostProfile(backend="job_server", server_url=url, server_allow_lan_http=True)
    restored = HostProfile.from_dict(host.to_dict())
    assert restored.server_allow_lan_http is True
    assert HostProfile.from_dict({"name": "old"}).server_allow_lan_http is False


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "compute.local",
        "192.168.1.2.evil.org",
        "[fe80::1]",
        "[::ffff:192.168.1.2]",
    ],
)
def test_opt_in_does_not_allow_public_dns_or_special_http_addresses(address):
    with pytest.raises(ValueError):
        validate_server_url(f"http://{address}:8080", True)


def test_backend_passes_opt_in_to_client():
    pytest.importorskip("cryptography")
    host = HostProfile(
        backend="job_server", server_url="http://192.168.1.2:8080", server_allow_lan_http=True
    )
    with (
        patch("job_manager.server_backend.require_client"),
        patch("job_manager.job_server_client.client.JobServerClient") as client,
    ):
        backend_for(host)
    assert client.call_args.kwargs == {"reuse_auth": True, "allow_lan_http": True}


def test_lan_http_client_bypasses_environment_proxy_and_keeps_redirect_refusal(monkeypatch):
    pytest.importorskip("cryptography")
    from urllib.request import ProxyHandler
    from job_manager.job_server_client.client import ClientError, JobServerClient, NoRedirect

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.org:8080")
    client = JobServerClient("http://192.168.1.2:8080", allow_lan_http=True)
    assert not any(isinstance(h, ProxyHandler) and h.proxies for h in client.opener.handlers)
    with pytest.raises(ClientError, match="redirects are forbidden"):
        NoRedirect().redirect_request(None, None, 302, "", {}, "http://8.8.8.8")


def test_enrollment_uses_same_opt_in_for_validation_and_network(tmp_path):
    pytest.importorskip("cryptography")
    pytest.importorskip("PySide6")
    from job_manager import server_backend

    field = Mock()
    field.text.return_value = str(tmp_path / "device.key")
    requests = []
    with (
        patch.object(server_backend, "require_client"),
        patch.object(
            server_backend, "server_credentials", return_value=("fixture", "synthetic-password")
        ),
        patch("job_manager.tasks.run_async", side_effect=lambda pool, work, **kw: work()),
        patch("job_manager.job_server_client.client.JobServerClient") as client,
    ):
        server_backend.show_enrollment(
            None, "http://192.168.1.2:8080", field, "", allow_lan_http=True
        )
        requests.append(client.call_args)
    assert requests[0].kwargs == {"allow_lan_http": True}
