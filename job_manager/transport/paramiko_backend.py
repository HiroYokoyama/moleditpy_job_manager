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


def ssh_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh", "config")


def ssh_config_for(hostname: str) -> dict:
    """The ``~/.ssh/config`` stanza for ``hostname``, or {} if there is none.

    The OpenSSH backend gets this for free by shelling out to ``ssh``; paramiko
    reads no config at all, so a host that only works because of an alias, a
    per-host ``User``, ``Port`` or ``IdentityFile`` would fail here for reasons
    the user cannot see. ProxyJump is deliberately *not* honoured: paramiko
    needs a real channel for it, and silently ignoring the bastion would be
    worse than the explicit error the caller raises.
    """
    config_class = getattr(paramiko, "SSHConfig", None)
    if config_class is None:
        return {}
    path = ssh_config_path()
    if not os.path.exists(path):
        return {}
    config = config_class()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            config.parse(handle)
    except (OSError, ValueError, paramiko.SSHException):
        logging.warning("Job Manager: could not parse %s", path)
        return {}
    return dict(config.lookup(hostname) or {})


class ParamikoTransport(Transport):
    """One long-lived SSHClient per host, guarded by a lock."""

    def __init__(self, host, password: Optional[str] = None) -> None:
        super().__init__(host)
        self.password = password
        self._client = None
        self._sftp = None
        #: The remote home directory, resolved once per connection.
        self._home = ""
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
        # The profile always wins; ssh_config only fills in what it leaves blank.
        ssh_config = ssh_config_for(host.hostname)
        kwargs = {
            "hostname": ssh_config.get("hostname") or host.hostname,
            "port": int(host.port or ssh_config.get("port") or 22),
            "username": host.username or ssh_config.get("user") or None,
            "timeout": int(host.connect_timeout or 10),
            "allow_agent": True,
            "look_for_keys": True,
        }
        if host.jump_host or ssh_config.get("proxyjump"):
            raise TransportError(
                "The paramiko backend cannot use a jump host. Use the OpenSSH "
                "backend for this host, which honours ProxyJump from ~/.ssh/config."
            )
        identities = ssh_config.get("identityfile") or []
        if host.key_path:
            kwargs["key_filename"] = os.path.expanduser(host.key_path)
        elif identities:
            kwargs["key_filename"] = [os.path.expanduser(p) for p in identities]
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
                # A reconnection may land on a different machine behind the same
                # name, so a home directory learnt from the old one is dropped.
                self._home = ""
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
        """SFTP has no shell, so ``~`` must be resolved before use.

        Cached for the life of the connection. Every remote path this plugin
        builds starts at ``~/moleditpy_jobs``, so without this a submission ran
        one extra command per uploaded file, and a download ran one per fetched
        result -- to ask an unchanging question.
        """
        if not path.startswith("~"):
            return path
        with self._lock:
            if not self._home:
                result = self.run('printf %s "$HOME"', timeout=20)
                self._home = (result.stdout or "").strip()
            if not self._home:
                raise TransportError("Could not resolve the remote home directory")
            return self._home + path[1:]

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
            self._home = ""


def trust_host_key(hostname: str, port: int = 22) -> str:
    """Append the host's current key to ``known_hosts``; returns its fingerprint.

    Only called after the user explicitly confirms the fingerprint shown by the
    Hosts dialog.
    """
    if paramiko is None:
        raise TransportError(INSTALL_HINT)
    # Resolve through ssh_config first: the fingerprint has to be filed under
    # the name the connection will actually verify, not under an alias.
    ssh_config = ssh_config_for(hostname)
    hostname = ssh_config.get("hostname") or hostname
    port = int(port or ssh_config.get("port") or 22)
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
