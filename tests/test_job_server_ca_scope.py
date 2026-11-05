"""The vendored client refuses a CA that is not limited to local names; no store is touched."""

from datetime import datetime, timedelta, timezone
from ipaddress import ip_network
from types import SimpleNamespace

import pytest

x509 = pytest.importorskip("cryptography.x509")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

LOCAL_IP = x509.IPAddress(ip_network("192.168.0.0/16"))
INCOMPLETE = [
    pytest.param([x509.IPAddress(ip_network("10.0.0.0/8"))], id="ip-only"),
    pytest.param([x509.DNSName("localhost")], id="dns-only"),
    pytest.param([x509.DNSName("your-pc.local")], id="local-name-only"),
]


def synthetic_ca(permitted):
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        certificate.fingerprint(hashes.SHA256()).hex(),
    )


@pytest.mark.parametrize("permitted", INCOMPLETE)
def test_incomplete_ca_is_refused_before_anything_is_trusted(tmp_path, monkeypatch, permitted):
    from job_manager import server_backend

    pem, fingerprint = synthetic_ca(permitted)
    opened = []
    monkeypatch.setattr("job_manager.job_server_client.trust.os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        "job_manager.job_server_client.trust.ctypes.WinDLL",
        lambda *args, **kwargs: opened.append(args),
        raising=False,
    )
    with pytest.raises(ValueError, match="must be limited"):
        server_backend.install_server_ca(
            "https://192.168.1.20:8767", pem, fingerprint, tmp_path, windows_store=True
        )
    assert opened == []
    assert not list(tmp_path.rglob("*.pem"))


@pytest.mark.parametrize("name", ["localhost", "your-pc.local"])
def test_ca_for_local_addresses_and_one_name_is_accepted(name):
    from job_manager.job_server_client.trust import validate_ca

    pem, fingerprint = synthetic_ca([LOCAL_IP, x509.DNSName(name)])
    assert validate_ca(pem, fingerprint)


@pytest.mark.parametrize("name", ["local", "example.com"])
def test_ca_for_a_wider_name_is_refused(name):
    from job_manager.job_server_client.trust import validate_ca

    pem, fingerprint = synthetic_ca([LOCAL_IP, x509.DNSName(name)])
    with pytest.raises(ValueError, match="must be limited"):
        validate_ca(pem, fingerprint)


def test_ca_for_two_local_names_is_refused():
    from job_manager.job_server_client.trust import validate_ca

    pem, fingerprint = synthetic_ca(
        [LOCAL_IP, x509.DNSName("your-pc.local"), x509.DNSName("other-pc.local")]
    )
    with pytest.raises(ValueError, match="must be limited"):
        validate_ca(pem, fingerprint)


def test_local_name_urls_follow_the_lan_http_opt_in():
    from job_manager.job_server_client.connection import validate_server_url

    assert validate_server_url("https://your-pc.local:8767") == "https://your-pc.local:8767"
    assert validate_server_url("http://your-pc.local:8766", True) == "http://your-pc.local:8766"
    with pytest.raises(ValueError):
        validate_server_url("http://your-pc.local:8766")
