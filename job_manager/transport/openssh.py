"""Transport that shells out to the system OpenSSH client.

Chosen as the default because it inherits everything the user has already
configured: ``~/.ssh/config``, agent keys, ``ProxyJump`` bastions, per-host
options. It needs no third-party package -- ``ssh``/``scp`` ship with Windows
10+, macOS and every Linux distribution.

``BatchMode=yes`` is deliberate. Without it a host that wants a password would
silently block a worker thread on a prompt nobody can see. With it, ssh fails
fast and the user is told to use a key, an agent, or the paramiko backend.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from typing import List, Optional

from .. import remote_paths
from .base import CommandResult, HostKeyRejected, Transport, TransportError

#: ControlMaster multiplexing needs a unix socket; OpenSSH-for-Windows has none.
SUPPORTS_MULTIPLEXING = os.name != "nt"

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class OpenSSHTransport(Transport):
    """Runs one ``ssh``/``scp`` process per operation."""

    def __init__(self, host, ssh_exe: str = "ssh", scp_exe: str = "scp") -> None:
        super().__init__(host)
        self.ssh_exe = ssh_exe
        self.scp_exe = scp_exe
        self._control_dir: Optional[str] = None

    # --- option assembly ----------------------------------------------------

    def _control_path(self) -> Optional[str]:
        if not SUPPORTS_MULTIPLEXING:
            return None
        if self._control_dir is None:
            self._control_dir = tempfile.mkdtemp(prefix="moleditpy_ssh_")
        return os.path.join(self._control_dir, "cm-%r@%h:%p")

    def _common_options(self) -> List[str]:
        host = self.host
        opts: List[str] = [
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(host.connect_timeout or 10)}",
        ]
        control_path = self._control_path()
        if control_path:
            opts += [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={control_path}",
                "-o",
                "ControlPersist=300",
            ]
        if host.key_path:
            opts += ["-i", host.key_path, "-o", "IdentitiesOnly=yes"]
        if host.jump_host:
            opts += ["-J", host.jump_host]
        for option in host.ssh_options or []:
            option = option.strip()
            if option:
                opts += ["-o", option]
        return opts

    def _ssh_argv(self, cmd: str) -> List[str]:
        host = self.host
        argv = [self.ssh_exe] + self._common_options()
        if host.port and int(host.port) != 22:
            argv += ["-p", str(int(host.port))]
        argv += [host.target, "--", cmd]
        return argv

    def _scp_argv(self, source: str, destination: str) -> List[str]:
        host = self.host
        argv = [self.scp_exe, "-q"] + self._common_options()
        if host.port and int(host.port) != 22:
            # scp spells the port -P, unlike ssh.
            argv += ["-P", str(int(host.port))]
        argv += [source, destination]
        return argv

    # --- execution ----------------------------------------------------------

    def _spawn(self, argv: List[str], timeout: int, what: str) -> CommandResult:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise TransportError(
                f"{argv[0]} not found. Install an OpenSSH client or use the paramiko backend."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"{what} timed out after {timeout}s") from exc

        stderr = proc.stderr or ""
        if proc.returncode == 255 or "Host key verification failed" in stderr:
            lowered = stderr.lower()
            if "host key verification failed" in lowered or "known_hosts" in lowered:
                raise HostKeyRejected(self.host.hostname, stderr.strip())
            if "permission denied" in lowered or "authentication" in lowered:
                raise TransportError(
                    "SSH authentication failed. The OpenSSH backend runs in batch mode "
                    "(key or agent only) -- switch this host to the paramiko backend "
                    f"for password login.\n{stderr.strip()[:300]}"
                )
        return CommandResult(proc.returncode, proc.stdout or "", stderr)

    def run(self, cmd: str, timeout: Optional[int] = None) -> CommandResult:
        wrapped = remote_paths.wrap_login(cmd, self.host.login_commands)
        limit = int(timeout or self.host.command_timeout or 60)
        return self._spawn(self._ssh_argv(wrapped), limit, "remote command")

    def _remote_spec(self, remote_path: str) -> str:
        # scp needs host:path; the path half is quoted by the remote shell, so
        # protect it here rather than relying on the local shell (there is none).
        return f"{self.host.target}:{remote_paths.quote(remote_path)}"

    def upload(self, local_path: str, remote_path: str) -> None:
        argv = self._scp_argv(local_path, self._remote_spec(remote_path))
        result = self._spawn(argv, int(self.host.command_timeout or 60) * 10, "upload")
        if not result.ok:
            raise TransportError(
                f"Upload of {os.path.basename(local_path)} failed: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )

    def download(self, remote_path: str, local_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        argv = self._scp_argv(self._remote_spec(remote_path), local_path)
        result = self._spawn(argv, int(self.host.command_timeout or 60) * 10, "download")
        if not result.ok:
            raise TransportError(
                f"Download of {remote_path} failed: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )

    def close(self) -> None:
        control_path = self._control_path()
        if control_path:
            try:
                subprocess.run(
                    [
                        self.ssh_exe,
                        "-O",
                        "exit",
                        "-o",
                        f"ControlPath={control_path}",
                        self.host.target,
                    ],
                    capture_output=True,
                    timeout=10,
                    creationflags=_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                logging.debug("Job Manager: ControlMaster exit failed", exc_info=True)
        if self._control_dir and os.path.isdir(self._control_dir):
            try:
                os.rmdir(self._control_dir)
            except OSError:
                logging.debug("Job Manager: control dir not empty: %s", self._control_dir)
        self._control_dir = None
