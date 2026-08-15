"""Transport that runs everything on this machine, with no SSH at all.

For the common case of a workstation that *is* the compute machine: the same
submit / poll / fetch / chain workflow, minus the network. Everything above
this layer is unchanged, because the only thing it asks of a transport is to
run a command and move a file.

Which shell it uses follows the host's scheduler, because that is what decides
the language of every command the plugin sends. A ``windows`` host is driven
entirely through PowerShell and needs nothing installed; every other scheduler
generates bash -- free on macOS and Linux, and on Windows meaning Git Bash or
WSL, which is why :func:`find_shell` looks for one rather than assuming it.

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

#: Windows ships ``System32\bash.exe`` as the WSL launcher rather than as a
#: POSIX shell, and it is ahead of Git's bash on PATH. It cannot see a Windows
#: path at all -- every job directory handed to it comes back as "wsl: Failed to
#: translate 'G:\\...'" -- so a host that picked it up would report a working
#: shell and then fail every single job.
#: Built with backslashes explicitly: os.path.join uses "/" off Windows, so on
#: a Linux CI runner this constant and the path being compared were separated
#: by nothing but the separator.
_WSL_LAUNCHER = os.path.normcase(
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bash.exe").replace(
        "/", "\\"
    )
)

#: Where PowerShell lives when it is not on PATH. Windows PowerShell 5.1 ships
#: with the OS, so on Windows this practically always resolves.
_POWERSHELL_CANDIDATES = (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: The two command languages this transport can speak.
SHELL_POSIX = "posix"
SHELL_POWERSHELL = "powershell"

INSTALL_HINT = (
    "This host needs a POSIX shell. Install Git for Windows (which provides "
    "bash) or use WSL; macOS and Linux already have one. To stay on Windows "
    "with nothing to install, set the host's scheduler to \"Built-in (Windows, "
    'PowerShell)" instead.'
)

POWERSHELL_HINT = (
    "PowerShell was not found. It ships with Windows; on macOS and Linux "
    "install PowerShell 7 (pwsh), or choose a scheduler that uses bash."
)


def find_powershell() -> str:
    """Path to a usable PowerShell, or "" when there is none."""
    # pwsh first: where both exist, PowerShell 7 is the better host, and 5.1 is
    # the fallback that happens to be everywhere.
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _POWERSHELL_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ""


def is_wsl_launcher(path: str) -> bool:
    """True for Windows' own ``bash.exe``, which is WSL's front door."""
    if not path:
        return False
    # Compared as written rather than through abspath, so the answer does not
    # depend on the platform the check happens to run on.
    return os.path.normcase(path.replace("/", "\\")) == _WSL_LAUNCHER


def find_shell(kind: str = SHELL_POSIX) -> str:
    """Path to a usable shell of this kind, or "" when there is none."""
    if kind == SHELL_POWERSHELL:
        return find_powershell()
    found = shutil.which("bash")
    if found and not is_wsl_launcher(found):
        return found
    for candidate in _WINDOWS_BASH_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ""


def shell_kind_for(host) -> str:
    """Which language this host's commands are written in.

    The scheduler decides: it is what generates the wrapper script and every
    status and cancel command, so the transport must not pick independently.
    """
    from ..models import SCHEDULER_WINDOWS

    return SHELL_POWERSHELL if getattr(host, "scheduler", "") == SCHEDULER_WINDOWS else SHELL_POSIX


def shell_available(kind: str = SHELL_POSIX) -> bool:
    return bool(find_shell(kind))


class LocalTransport(Transport):
    """Runs commands through a local bash; copies files instead of scp."""

    def __init__(self, host, shell: str = "", kind: str = "") -> None:
        super().__init__(host)
        self.kind = kind or shell_kind_for(host)
        self._shell = shell or find_shell(self.kind)

    # --- helpers ------------------------------------------------------------

    def _require_shell(self) -> str:
        if not self._shell:
            raise TransportError(POWERSHELL_HINT if self.kind == SHELL_POWERSHELL else INSTALL_HINT)
        return self._shell

    def _argv(self, command: str) -> List[str]:
        if self.kind == SHELL_POWERSHELL:
            # -NonInteractive so a cmdlet that wants confirmation fails instead
            # of blocking a worker thread on a prompt nobody can see.
            return [self._shell, "-NoProfile", "-NonInteractive", "-Command", command]
        # -l so the user's profile is sourced: a login node's modules and PATH
        # live there, and a job that cannot find its program is the usual
        # symptom of skipping it.
        return [self._shell, "-lc", command]

    def _resolve(self, path: str) -> str:
        """Expand a job path the way the local shell would."""
        return os.path.abspath(os.path.expanduser(path or ""))

    # --- operations ---------------------------------------------------------

    def run(self, cmd: str, timeout: Optional[int] = None) -> CommandResult:
        from .. import remote_paths

        self._require_shell()
        wrapped = remote_paths.wrap_login(cmd, self.host.environment_commands())
        limit = int(timeout or self.host.command_timeout or 60)
        argv: List[str] = self._argv(wrapped)
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
        if self.kind == SHELL_POWERSHELL:
            probe = "'moleditpy_ok'; [System.Net.Dns]::GetHostName()"
        else:
            probe = "echo moleditpy_ok && hostname"
        result = self.run(probe, timeout=20)
        result.check("local shell test")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return lines[-1].strip() if lines else "this machine"

    def close(self) -> None:
        """Nothing is held open."""


__all__ = [
    "INSTALL_HINT",
    "POWERSHELL_HINT",
    "SHELL_POSIX",
    "SHELL_POWERSHELL",
    "LocalTransport",
    "find_powershell",
    "find_shell",
    "shell_available",
    "shell_kind_for",
]
