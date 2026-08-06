"""Transport that runs everything on this machine, with no SSH at all.

For the common case of a workstation that *is* the compute machine: the same
submit / poll / fetch / chain workflow, minus the network. Everything above
this layer is unchanged, because the only thing it asks of a transport is to
run a command and move a file.

A POSIX shell is still required -- the generated run script is bash, and the
plugin's remote commands are ``mkdir -p``, ``ls``, ``kill -0``, ``tail``. That
is free on macOS and Linux; on Windows it means Git Bash (or WSL), which is why
:func:`find_shell` looks for ``bash`` rather than assuming one.

"Upload" and "download" are file copies. When the job directory and the chosen
download directory are the same file, the copy is skipped rather than
truncating the file onto itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional

from .base import CommandResult, Transport, TransportError

#: Where a bash lives on Windows when it is not on PATH.
_WINDOWS_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

INSTALL_HINT = (
    "The local backend needs a POSIX shell. Install Git for Windows (which "
    "provides bash) or use WSL; macOS and Linux already have one."
)


def find_shell() -> str:
    """Path to a usable bash, or "" when there is none."""
    found = shutil.which("bash")
    if found:
        return found
    for candidate in _WINDOWS_BASH_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ""


def shell_available() -> bool:
    return bool(find_shell())


class LocalTransport(Transport):
    """Runs commands through a local bash; copies files instead of scp."""

    def __init__(self, host, shell: str = "") -> None:
        super().__init__(host)
        self._shell = shell or find_shell()

    # --- helpers ------------------------------------------------------------

    def _require_shell(self) -> str:
        if not self._shell:
            raise TransportError(INSTALL_HINT)
        return self._shell

    def _resolve(self, path: str) -> str:
        """Expand a job path the way the local shell would."""
        return os.path.abspath(os.path.expanduser(path or ""))

    # --- operations ---------------------------------------------------------

    def run(self, cmd: str, timeout: Optional[int] = None) -> CommandResult:
        from .. import remote_paths

        shell = self._require_shell()
        wrapped = remote_paths.wrap_login(cmd, self.host.login_commands)
        limit = int(timeout or self.host.command_timeout or 60)
        argv: List[str] = [shell, "-lc", wrapped]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise TransportError(INSTALL_HINT) from exc
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"Local command timed out after {limit}s") from exc
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def upload(self, local_path: str, remote_path: str) -> None:
        self._copy(local_path, self._resolve(remote_path), "Upload")

    def download(self, remote_path: str, local_path: str) -> None:
        self._copy(self._resolve(remote_path), local_path, "Download")

    def _copy(self, source: str, destination: str, what: str) -> None:
        try:
            if os.path.exists(destination) and os.path.samefile(source, destination):
                # Job directory and download directory are the same place;
                # copying would truncate the file onto itself.
                return
            os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise TransportError(f"{what} of {os.path.basename(source)} failed: {exc}") from exc

    def test_connection(self) -> str:
        """No connection to make; report the shell and the machine name."""
        self._require_shell()
        result = self.run("echo moleditpy_ok && hostname", timeout=20)
        result.check("local shell test")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return lines[-1].strip() if lines else "this machine"

    def close(self) -> None:
        """Nothing is held open."""


__all__ = ["INSTALL_HINT", "LocalTransport", "find_shell", "shell_available"]
