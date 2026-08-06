"""Transport abstraction: run a command, move a file, nothing else.

Two implementations exist (``openssh`` and ``paramiko_backend``) and a host
profile selects one. Everything above this layer -- schedulers, poller, UI --
talks only to this interface, which is also what makes the whole stack
testable offline against a fake transport.

All methods block and must therefore only ever be called from a worker thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class TransportError(RuntimeError):
    """Connection, authentication or transfer failure."""


class HostKeyRejected(TransportError):
    """The remote host key is not in known_hosts, so the connection was refused."""

    def __init__(self, hostname: str, message: str = "") -> None:
        super().__init__(message or f"Unknown host key for {hostname}")
        self.hostname = hostname


@dataclass
class CommandResult:
    """Outcome of one remote command."""

    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def check(self, what: str = "remote command") -> "CommandResult":
        if not self.ok:
            detail = (self.stderr or self.stdout or "").strip()
            raise TransportError(f"{what} failed (rc={self.rc}): {detail[:500]}")
        return self


class Transport(ABC):
    """Blocking SSH operations against a single host."""

    def __init__(self, host) -> None:  # host: models.HostProfile
        self.host = host

    @abstractmethod
    def run(self, cmd: str, timeout: Optional[int] = None) -> CommandResult:
        """Execute ``cmd`` in a remote shell and collect its output."""

    @abstractmethod
    def upload(self, local_path: str, remote_path: str) -> None:
        """Copy one local file to ``remote_path``."""

    @abstractmethod
    def download(self, remote_path: str, local_path: str) -> None:
        """Copy one remote file to ``local_path``."""

    def mkdirs(self, remote_dir: str) -> None:
        from .. import remote_paths

        self.run(f"mkdir -p {remote_paths.quote(remote_dir)}").check("mkdir -p")

    def test_connection(self) -> str:
        """Round-trip a trivial command; returns the remote hostname."""
        result = self.run("echo moleditpy_ok && hostname", timeout=20)
        result.check("connection test")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return lines[-1].strip() if lines else ""

    def close(self) -> None:
        """Release any persistent connection. Safe to call repeatedly."""
