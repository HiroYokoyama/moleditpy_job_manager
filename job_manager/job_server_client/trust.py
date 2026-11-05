"""CA bootstrap confirmed by fingerprint; ordinary API connections always verify TLS."""

import ctypes
import hmac
import os
import re
import ssl
from datetime import datetime, timezone
from ipaddress import ip_network
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from .connection import validate_server_url


CA_PERMITTED_NETWORKS = tuple(
    ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
        "127.0.0.0/8",
        "::1/128",
    )
)
CA_PERMITTED_DNS_NAME = "localhost"


def normalize_fingerprint(value):
    value = "".join(str(value).split()).replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Paste the full SHA-256 fingerprint shown on the server PC")
    return value


def _require_local_name_constraints(certificate):
    """A trusted local CA must be unable to issue for public names or addresses."""
    message = "The CA must be limited to private addresses and localhost. No trust was installed."
    try:
        extension = certificate.extensions.get_extension_for_class(x509.NameConstraints)
    except x509.ExtensionNotFound:
        raise ValueError(message) from None
    permitted = extension.value.permitted_subtrees
    if not extension.critical or not permitted:
        raise ValueError(message)
    for name in permitted:
        if isinstance(name, x509.DNSName):
            allowed = name.value == CA_PERMITTED_DNS_NAME
        else:
            allowed = isinstance(name, x509.IPAddress) and name.value in CA_PERMITTED_NETWORKS
        if not allowed:
            raise ValueError(message)


def certificate_fingerprint(pem):
    """The SHA-256 fingerprint in the form the server PC displays."""
    return validate_ca(pem).fingerprint(hashes.SHA256()).hex().upper()


def validate_ca(pem, expected_fingerprint=None):
    if not isinstance(pem, bytes) or len(pem) > 65536:
        raise ValueError("Invalid CA certificate size")
    certificates = x509.load_pem_x509_certificates(pem)
    if len(certificates) != 1:
        raise ValueError("Exactly one CA certificate is required")
    certificate = certificates[0]
    fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
    if expected_fingerprint is not None and not hmac.compare_digest(
        fingerprint, normalize_fingerprint(expected_fingerprint)
    ):
        raise ValueError("CA fingerprint differs from the server PC. No trust was installed.")
    certificate.verify_directly_issued_by(certificate)
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    if not constraints.ca:
        raise ValueError("The certificate is not a CA")
    if constraints.path_length != 0:
        raise ValueError("The CA must not allow intermediate CAs")
    _require_local_name_constraints(certificate)
    if not certificate.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
        raise ValueError("The CA certificate cannot sign certificates")
    now = datetime.now(timezone.utc)
    if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
        raise ValueError("The CA certificate is expired or not yet valid")
    return certificate


def fetch_server_ca(url, expected_fingerprint=None):
    """Fetch the public CA. Without a fingerprint, the caller must have the user confirm one."""
    from .client import NoRedirect

    if expected_fingerprint is not None:
        expected_fingerprint = normalize_fingerprint(expected_fingerprint)
    url = validate_server_url(url)
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("CA bootstrap requires an HTTPS server URL")
    # Only the explicit local/private-address exception may bootstrap trust.
    try:
        validate_server_url(parts._replace(scheme="http").geturl(), allow_lan_http=True)
    except ValueError:
        raise ValueError(
            "CA bootstrap requires localhost or a private LAN IP. Publicly trusted HTTPS does not need local CA registration."
        ) from None
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.check_hostname = False
    tls_context.verify_mode = ssl.CERT_NONE
    opener = build_opener(NoRedirect(), ProxyHandler({}), HTTPSHandler(context=tls_context))
    request = Request(url + "/api/v1/tls/ca", method="GET")
    with opener.open(request, timeout=5) as response:
        pem = response.read(65537)
    certificate = validate_ca(pem, expected_fingerprint)
    return certificate.public_bytes(serialization.Encoding.PEM)


def trust_windows_user_ca(pem, expected_fingerprint):
    """Explicitly trust the verified public CA in CurrentUser ROOT, never LocalMachine."""
    if os.name != "nt":
        raise ValueError("Windows user certificate trust is available only on Windows")
    from ctypes import wintypes

    certificate = validate_ca(pem, expected_fingerprint)
    der = certificate.public_bytes(serialization.Encoding.DER)
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt.CertOpenSystemStoreW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    crypt.CertOpenSystemStoreW.restype = wintypes.HANDLE
    crypt.CertAddEncodedCertificateToStore.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    crypt.CertAddEncodedCertificateToStore.restype = wintypes.BOOL
    crypt.CertCloseStore.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    crypt.CertCloseStore.restype = wintypes.BOOL
    store = crypt.CertOpenSystemStoreW(None, "ROOT")
    if not store:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(der)
        if not crypt.CertAddEncodedCertificateToStore(
            store, 1, ctypes.cast(buffer, ctypes.c_void_p), len(der), 3, None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        crypt.CertCloseStore(store, 0)
