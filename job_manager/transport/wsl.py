"""Run jobs inside a WSL distribution, from Windows, with no SSH.

The gap this fills: a Windows workstation with the calculation program
installed under Linux -- which is how most of them are actually shipped. The
local backend cannot reach it. Its bash lookup deliberately *rejects*
``System32\\bash.exe``, because that is WSL's launcher and not a POSIX shell:
handed a Windows job directory it answers "Failed to translate 'G:\\...'" and
fails every job. That rejection is right, and this is the other half of it --
the way to use WSL is to keep everything Linux side and translate only the
files that cross.

So a WSL host is a Linux host in every respect that matters here. Its remote
root is a Linux path, its scheduler is the ordinary shell one, its wrapper is
the same bash script a cluster gets, and ``wslpath`` is what turns a Windows
file into something ``cp`` can read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional

from .base import CommandResult, Transport, TransportError

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

INSTALL_HINT = (
    "WSL was not found. It is a Windows feature: run 'wsl --install' in a "
    "terminal, or choose a different backend."
)

NO_DISTRO_HINT = (
    "WSL is installed but has no distribution to run in. Install one from the "
    "Microsoft Store, or with 'wsl --install -d Ubuntu'."
)


def _quote(text: str) -> str:
    """POSIX single-quoting for a Windows path handed to a Linux shell.

    Not ``shlex.quote``: that leaves a path with no special character bare, and
    bare is exactly what a Windows path must never be here -- the backslashes
    would be read as escapes on the way in.
    """
    return "'" + str(text or "").replace("'", "'\"'\"'") + "'"


def find_wsl() -> str:
    """Path to ``wsl.exe``, or "" where there is none."""
    if sys.platform != "win32":
        return ""
    found = shutil.which("wsl")
    if found:
        return found
    fallback = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wsl.exe"
    ).replace("/", "\\")
    return fallback if os.path.isfile(fallback) else ""


def wsl_available() -> bool:
    return bool(find_wsl())


def list_distributions() -> List[str]:
    """The installed distributions, best-effort and never raising.

    ``wsl -l -q`` answers in UTF-16, which is why the output is decoded here
    rather than left to the default encoding -- read as UTF-8 every name comes
    back with a NUL between each letter.
    """
    exe = find_wsl()
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "-l", "-q"],
            capture_output=True,
            timeout=15,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = proc.stdout or b""
    for encoding in ("utf-16-le", "utf-8"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        names = [line.strip().strip("\x00") for line in text.splitlines()]
        found = [name for name in names if name]
        if found:
            return found
    return []


class WSLTransport(Transport):
    """Runs bash inside a distribution; copies files through ``wslpath``."""

    #: Every generated script is bash -- the wrapper uses shopt, traps and
    #: aliases, and it is the same script a cluster gets. A distribution
    #: without bash is told so by name rather than failing a command at a time.
    SHELL = "bash"

    def __init__(self, host, exe: str = "", shell: str = "") -> None:
        super().__init__(host)
        self._exe = exe or find_wsl()
        self.shell = shell or self.SHELL
        self.distro = (getattr(host, "wsl_distro", "") or "").strip()

    # --- helpers ------------------------------------------------------------

    def _require(self) -> str:
        if not self._exe:
            raise TransportError(INSTALL_HINT)
        return self._exe

    def _argv(self, command: str) -> List[str]:
        argv = [self._require()]
        if self.distro:
            argv += ["-d", self.distro]
        # --cd /, always. wsl.exe starts in the caller's working directory and
        # prints "Failed to translate <path>" to stderr when that directory is
        # not one WSL can see -- a network share, or a drive the distribution
        # does not mount. The message lands in the middle of the output being
        # parsed, and the job script cds where it needs to be anyway.
        # -c where the host reads the login files itself, for the reason
        # LocalTransport._argv gives: a login shell is 260 ms against 14 ms,
        # and the profile is already in the command.
        flag = "-c" if getattr(self.host, "load_profile", False) else "-lc"
        argv += ["--cd", "/", "--", self.shell, flag, command]
        return argv

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
            raise TransportError(INSTALL_HINT) from exc
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"{what} timed out after {timeout}s") from exc
        stderr = proc.stderr or ""
        lowered = stderr.lower()
        if "no installed distributions" in lowered or "wsl.exe -l" in lowered:
            raise TransportError(NO_DISTRO_HINT)
        if f"{self.shell}: not found" in lowered or f"{self.shell}: command not found" in lowered:
            raise TransportError(self.no_shell_hint())
        return CommandResult(proc.returncode, proc.stdout or "", stderr)

    def no_shell_hint(self) -> str:
        where = (
            f"The WSL distribution '{self.distro}'" if self.distro else "The default distribution"
        )
        return (
            f"{where} has no {self.shell}. The job script is a {self.shell} script -- "
            "the same one a cluster is sent -- so use a full distribution "
            "(Ubuntu, Debian, ...) rather than a container image."
        )

    def to_wsl_path(self, windows_path: str) -> str:
        """The Linux name for a Windows file, via the distribution's wslpath.

        Asked of WSL rather than worked out here: which drives are mounted, and
        under what prefix, is the distribution's own configuration (/mnt/c is a
        default, not a rule -- Docker Desktop's distribution uses /mnt/host/c),
        and a wrong guess writes a job's results somewhere nobody looks.

        The path is passed *inside* the shell command in single quotes, never
        as its own argument. ``wsl.exe`` eats the backslashes of an unquoted
        argument: ``C:\\Users`` arrives as ``C:Users`` and wslpath rejects it,
        while the same path with a space in it survives because the Windows
        command line quoted it -- so it worked or failed depending on the
        directory the user happened to be working in.
        """
        absolute = os.path.abspath(windows_path)
        result = self.run(f"wslpath -a -u {_quote(absolute)}", timeout=30)
        translated = (result.stdout or "").strip().splitlines()
        if not result.ok or not translated:
            raise TransportError(
                f"WSL cannot see {absolute}. A file has to be on a drive the "
                "distribution mounts; a network path is not."
            )
        return translated[-1].strip()

    # --- operations ---------------------------------------------------------

    def run(self, cmd: str, timeout: Optional[int] = None) -> CommandResult:
        from .. import remote_paths

        wrapped = remote_paths.wrap_login(cmd, self.host.environment_commands())
        limit = int(timeout or self.host.command_timeout or 60)
        return self._spawn(self._argv(wrapped), limit, "WSL command")

    def upload(self, local_path: str, remote_path: str) -> None:
        from .. import remote_paths

        source = self.to_wsl_path(local_path)
        target = remote_paths.quote(remote_path)
        result = self.run(
            f'mkdir -p -- "$(dirname {target})" && cp -f -- {remote_paths.quote(source)} {target}',
            timeout=max(60, int(self.host.command_timeout or 60) * 10),
        )
        if not result.ok:
            raise TransportError(
                f"Upload of {os.path.basename(local_path)} into WSL failed: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )

    def download(self, remote_path: str, local_path: str) -> None:
        from .. import remote_paths

        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        # The destination is created first: wslpath translates a path whether or
        # not it exists, but only if its directory does.
        target = self.to_wsl_path(local_path)
        result = self.run(
            f"cp -f -- {remote_paths.quote(remote_path)} {remote_paths.quote(target)}",
            timeout=max(60, int(self.host.command_timeout or 60) * 10),
        )
        if not result.ok:
            raise TransportError(
                f"Download of {remote_path} from WSL failed: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )

    def test_connection(self) -> str:
        # Asked outright, so a distribution that cannot run the job script says
        # so here rather than at the first submission.
        probe = self._spawn(
            self._argv(f"command -v {self.shell} >/dev/null || exit 127"), 30, "WSL test"
        )
        if probe.rc == 127:
            raise TransportError(self.no_shell_hint())
        result = self.run("echo moleditpy_ok && hostname", timeout=30)
        result.check("WSL test")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return lines[-1].strip() if lines else (self.distro or "WSL")

    def close(self) -> None:
        """Nothing is held open: each command is its own wsl.exe."""


__all__ = [
    "INSTALL_HINT",
    "NO_DISTRO_HINT",
    "WSLTransport",
    "find_wsl",
    "list_distributions",
    "wsl_available",
]
