"""Shared origin validation; LAN HTTP is an explicit exception for private addresses."""

import re
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

_MDNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local")


def is_mdns_name(host):
    """A one-label `.local` name, as a PC announces itself on the LAN."""
    return isinstance(host, str) and _MDNS_NAME.fullmatch(host) is not None


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
    lan = is_mdns_name(parts.hostname)
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
        raise ValueError("Use HTTPS, or enable LAN HTTP for a private IP address or a .local name")
    return parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), path="").geturl()
