"""Shared origin validation; LAN HTTP is an explicit, literal-address exception."""

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit


def validate_server_url(url, allow_lan_http=False):
    if type(allow_lan_http) is not bool:
        raise ValueError("allow_lan_http must be a boolean")
    parts = urlsplit(url)
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in {"", "/"}
            or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url):
        raise ValueError("Use a server origin without credentials, paths or query strings")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("Invalid server port")
    loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    lan = False
    try:
        address = ip_address(parts.hostname)
        lan = any(address in ip_network(network) for network in (
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"
        )) and "%" not in parts.hostname
    except ValueError:
        pass
    if parts.scheme != "https" and not (
        parts.scheme == "http" and (loopback or (allow_lan_http and lan))
    ):
        raise ValueError("Use HTTPS, or enable LAN HTTP for a private IPv4/IPv6 literal address")
    return parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), path="").geturl()
