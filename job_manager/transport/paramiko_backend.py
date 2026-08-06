"""Optional transport built on paramiko.

Needed only for hosts that require a password (the OpenSSH backend runs in
batch mode and cannot answer a prompt) or where one persistent connection is
preferable to a process per command.

paramiko is *not* declared in ``PLUGIN_DEPENDENCIES``: the default backend
needs nothing, so installing it for every user would be gratuitous. The import
is guarded and this module stays importable on a bare ``pip install pytest``.

Unknown host keys are **rejected**, never auto-added. The UI turns the
resulting :class:`HostKeyRejected` into an explicit confirmation before the
fingerprint is written to ``known_hosts``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from .. import remote_paths
from .base import CommandResult, HostKeyRejected, Transport, TransportError

try:
    import paramiko
except (ImportError, TypeError):  # pragma: no cover - exercised via the flag
    paramiko = None


PARAMIKO_AVAILABLE = paramiko is not None

INSTALL_HINT = "paramiko is not installed. Run: pip install paramiko"


def known_hosts_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")


class ParamikoTransport(Transport):
    """One long-lived SSHClient per host, guarded by a lock."""

    def __init__(self, host, password: Optional[str] = None) -> None:
        super().__init__(host)
        self.password = password
        self._client = None
        self._sftp = None
        self._lock = threading.RLock()

    # --- connection ---------------------------------------------------------

    def _connect(self):
        if paramiko is None:
            raise TransportError(INSTALL_HINT)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        user_known_hosts = known_hosts_path()
        if os.path.exists(user_known_hosts):
            try:
                client.load_host_keys(user_known_hosts)
            except (OSError, ValueError):
                logging.warning("Job Manager: could not parse %s", user_known_hosts)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        host = self.host
        kwargs = {
            "hostname": host.hostname,
            "port": int(host.port or 22),
            "username": host.username or None,
            "timeout": int(host.connect_timeout or 10),
            "allow_agent": True,
            "look_for_keys": True,
        }
        if host.key_path:
            kwargs["key_filename"] = os.path.expanduser(host.key_path)
        if self.password:
            kwargs["password"] = self.password

        try:
            client.connect(**kwargs)
        except paramiko.SSHException as exc:
            message = str(exc)
            if "not found in known_hosts" in message or isinstance(
                exc, paramiko.BadHostKeyException
            ):
                raise HostKeyRejected(host.hostname, message) from exc
            raise TransportError(f"SSH connection failed: {message}") from exc
        except OSError as exc:
            raise TransportError(f"SSH connection failed: {exc}") from exc
        return client

    def _ensure_client(self):
        with self._lock:
            transport = self._client.get_transport() if self._client else None
            if transport is None or not transport.is_active():
                self._sftp = None
                self._client = self._connect()
            return self._client

    def _ensure_sftp(self):
        with self._lock:
            client = self._ensure_client()
            if self._sftp is None:
                self._sftp = client.open_sftp()
            return self._sftp

    # --- operations ---------------------------------------------------------

    def run(self, cmd: str, timeout: Optional[int] = None) -> CommandResult:
        wrapped = remote_paths.wrap_login(cmd, self.host.login_commands)
        limit = int(timeout or self.host.command_timeout or 60)
        with self._lock:
            client = self._ensure_client()
            try:
                _stdin, stdout, stderr = client.exec_command(wrapped, timeout=limit)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                rc = stdout.channel.recv_exit_status()
            except Exception as exc:  # paramiko raises a wide family here
                raise TransportError(f"Remote command failed: {exc}") from exc
        return CommandResult(rc, out, err)

    def _expand_remote(self, path: str) -> str:
        """SFTP has no shell, so ``~`` must be resolved before use."""
        if not path.startswith("~"):
            return path
        result = self.run('printf %s "$HOME"', timeout=20)
        home = (result.stdout or "").strip()
        if not home:
            raise TransportError("Could not resolve the remote home directory")
        return home + path[1:]

    def upload(self, local_path: str, remote_path: str) -> None:
        sftp = self._ensure_sftp()
        try:
            sftp.put(local_path, self._expand_remote(remote_path))
        except OSError as exc:
            raise TransportError(f"Upload of {os.path.basename(local_path)} failed: {exc}") from exc

    def download(self, remote_path: str, local_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        sftp = self._ensure_sftp()
        try:
            sftp.get(self._expand_remote(remote_path), local_path)
        except OSError as exc:
            raise TransportError(f"Download of {remote_path} failed: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            for handle in (self._sftp, self._client):
                try:
                    if handle is not None:
                        handle.close()
                except Exception:
                    logging.debug("Job Manager: transport close failed", exc_info=True)
            self._sftp = None
            self._client = None


def trust_host_key(hostname: str, port: int = 22) -> str:
    """Append the host's current key to ``known_hosts``; returns its fingerprint.

    Only called after the user explicitly confirms the fingerprint shown by the
    Hosts dialog.
    """
    if paramiko is None:
        raise TransportError(INSTALL_HINT)
    transport = None
    sock = None
    try:
        import socket

        sock = socket.create_connection((hostname, int(port or 22)), timeout=10)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=10)
        key = transport.get_remote_server_key()
    except Exception as exc:
        raise TransportError(f"Could not read host key for {hostname}: {exc}") from exc
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                logging.debug("Job Manager: host-key probe close failed", exc_info=True)
        elif sock is not None:
            try:
                sock.close()
            except OSError:
                logging.debug("Job Manager: host-key probe socket close failed")

    path = known_hosts_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    host_keys = paramiko.HostKeys()
    if os.path.exists(path):
        try:
            host_keys.load(path)
        except (OSError, ValueError):
            logging.warning("Job Manager: could not parse %s before adding a key", path)
    entry = hostname if int(port or 22) == 22 else f"[{hostname}]:{int(port)}"
    host_keys.add(entry, key.get_name(), key)
    host_keys.save(path)
    return key.get_fingerprint().hex()
