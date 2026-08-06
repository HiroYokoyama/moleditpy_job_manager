"""Transport backends and the factory that picks one for a host."""

from __future__ import annotations

from typing import Optional

from ..models import BACKEND_LOCAL, BACKEND_PARAMIKO, HostProfile
from .base import CommandResult, HostKeyRejected, Transport, TransportError
from .openssh import OpenSSHTransport

__all__ = [
    "CommandResult",
    "HostKeyRejected",
    "Transport",
    "TransportError",
    "OpenSSHTransport",
    "create_transport",
    "local_shell_available",
    "paramiko_available",
]


def paramiko_available() -> bool:
    from .paramiko_backend import PARAMIKO_AVAILABLE

    return PARAMIKO_AVAILABLE


def local_shell_available() -> bool:
    from .local import shell_available

    return shell_available()


def create_transport(host: HostProfile, password: Optional[str] = None) -> Transport:
    """Instantiate the backend named by ``host.backend``."""
    if host.backend == BACKEND_LOCAL:
        from .local import LocalTransport

        return LocalTransport(host)
    if host.backend == BACKEND_PARAMIKO:
        from .paramiko_backend import INSTALL_HINT, ParamikoTransport, PARAMIKO_AVAILABLE

        if not PARAMIKO_AVAILABLE:
            raise TransportError(INSTALL_HINT)
        return ParamikoTransport(host, password=password)
    return OpenSSHTransport(host)
