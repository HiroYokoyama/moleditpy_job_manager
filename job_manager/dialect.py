"""The handful of commands the plugin runs that are not the job itself.

Making a directory, reading a sentinel, listing outputs, tailing a log: four
things every backend needs and none of them the scheduler's business. They were
POSIX because everything was POSIX, and the native Windows backend is exactly
the case that breaks -- a wrapper written in PowerShell is no use if the
plugin then asks for ``mkdir -p``.

So the dialect follows the *scheduler*, not the transport. The scheduler is
what decides the language of the wrapper and of every status and cancel
command; having the transport choose independently is how the two end up
disagreeing.

``~`` is deliberately handled differently in each. A POSIX shell expands it,
and quoting the whole path would turn it into a literal directory called ``~``.
PowerShell does not expand it at all in a quoted string, so it is resolved to
the user's profile directory before quoting.
"""

from __future__ import annotations

import base64
import os
import shlex
from typing import List, Sequence


#: What :meth:`Dialect.read_files` prints for a file that is not there. The
#: poller turns exactly this word into LOST -- it is how a job killed before
#: its wrapper finished is told apart from one that ended -- so both dialects
#: and the classifier have to agree on it, and none of them may spell it
#: independently.
MISSING = "MISSING"

#: What :meth:`Dialect.exists` prints for a path that is there. Checked before
#: a job is submitted into a directory the user typed, where the alternative is
#: ``mkdir -p`` quietly making the typo and the job running in an empty
#: directory that has none of the files it was supposed to find.
PRESENT = "PRESENT"


class Dialect:
    """One shell's spelling of the plugin's own housekeeping commands."""

    name = "posix"

    def quote(self, path: str) -> str:
        text = path or ""
        if text == "~":
            return "~"
        if text.startswith("~/"):
            rest = text[2:]
            # Quote only what follows the tilde, or the shell stops expanding it.
            return "~/" + shlex.quote(rest) if rest else "~/"
        return shlex.quote(text)

    def mkdirs(self, path: str) -> str:
        return f"mkdir -p {self.quote(path)}"

    def exists(self, path: str, directory: bool = False) -> str:
        """Print :data:`PRESENT` or :data:`MISSING` for one path."""
        test = "-d" if directory else "-f"
        return f"if [ {test} {self.quote(path)} ]; then echo {PRESENT}; else echo {MISSING}; fi"

    def read_files(self, paths: Sequence[str], mark: str) -> str:
        """One command that prints every file, separated by ``mark``."""
        parts = [
            f'echo "{mark}"; cat {self.quote(path)} 2>/dev/null || echo {MISSING}' for path in paths
        ]
        return "; ".join(parts)

    def list_dir(self, path: str) -> str:
        """List a directory, marking sub-directories with a trailing slash."""
        return f"ls -p -1 {self.quote(path)} 2>/dev/null || true"

    def list_tree(self, path: str, depth: int) -> str:
        """Files up to ``depth`` levels down, named relative to ``path``.

        Run from inside the directory so the names come back relative with no
        printf extension: -printf is GNU find only, and a cluster login node is
        as likely to be BSD or macOS.
        """
        return (
            f"cd {self.quote(path)} 2>/dev/null && "
            f"find . -maxdepth {int(depth)} -type f 2>/dev/null | sed 's|^\\./||' || true"
        )

    def tail(self, path: str, lines: int) -> str:
        return f"tail -n {int(lines)} {self.quote(path)} 2>&1 || true"

    def run_in(self, directory: str, command: str) -> str:
        """Run a command with the job directory as the working directory."""
        return f"cd {self.quote(directory)} && {command}"

    def probe(self) -> str:
        return "echo moleditpy_ok && hostname"


class PowerShellDialect(Dialect):
    """The same four things, for a host with no POSIX shell at all."""

    name = "powershell"

    def quote(self, path: str) -> str:
        text = str(path or "")
        if text == "~" or text.startswith("~/") or text.startswith("~\\"):
            # PowerShell does not expand ~ inside a quoted string, and leaving
            # it unquoted is not an option for a path that may contain spaces.
            text = os.path.expanduser(text.replace("/", os.sep))
        return "'" + text.replace("'", "''") + "'"

    def mkdirs(self, path: str) -> str:
        # -Force is the -p: it creates parents and is silent when the directory
        # already exists, which every caller relies on.
        return f"New-Item -ItemType Directory -Force -Path {self.quote(path)} | Out-Null"

    def exists(self, path: str, directory: bool = False) -> str:
        kind = "Container" if directory else "Leaf"
        quoted = self.quote(path)
        return (
            f"if (Test-Path -LiteralPath {quoted} -PathType {kind}) "
            f"{{ '{PRESENT}' }} else {{ '{MISSING}' }}"
        )

    def read_files(self, paths: Sequence[str], mark: str) -> str:
        parts: List[str] = []
        for path in paths:
            quoted = self.quote(path)
            parts.append(
                f"Write-Output '{mark}'; "
                f"if (Test-Path -LiteralPath {quoted}) "
                f"{{ Get-Content -LiteralPath {quoted} }} "
                f"else {{ Write-Output '{MISSING}' }}"
            )
        return "; ".join(parts)

    def list_dir(self, path: str) -> str:
        quoted = self.quote(path)
        return (
            f"if (Test-Path -LiteralPath {quoted}) {{ "
            f"Get-ChildItem -LiteralPath {quoted} -Force | ForEach-Object {{ "
            "if ($_.PSIsContainer) { $_.Name + '/' } else { $_.Name } } }"
        )

    def list_tree(self, path: str, depth: int) -> str:
        """The same, recursively, named relative to ``path``.

        Reported with forward slashes, so one matcher serves both dialects and
        a pattern written for a cluster means the same thing on Windows.
        """
        quoted = self.quote(path)
        separator = "'\\'"
        return (
            f"if (Test-Path -LiteralPath {quoted}) {{ "
            f"$root = (Resolve-Path -LiteralPath {quoted}).Path.TrimEnd({separator}) "
            f"+ {separator}; "
            f"Get-ChildItem -LiteralPath {quoted} -Force -Recurse -Depth {int(depth) - 1} "
            "-File | ForEach-Object { "
            f"$_.FullName.Substring($root.Length).Replace({separator}, '/') }} }}"
        )

    def tail(self, path: str, lines: int) -> str:
        quoted = self.quote(path)
        return (
            f"if (Test-Path -LiteralPath {quoted}) "
            f"{{ Get-Content -LiteralPath {quoted} -Tail {int(lines)} }}"
        )

    def run_in(self, directory: str, command: str) -> str:
        # `;` not `&&`: Windows PowerShell 5.1 has no pipeline chain operators
        # at all, and `&&` there is a parser error rather than a no-op.
        return f"Set-Location -LiteralPath {self.quote(directory)}; {command}"

    def probe(self) -> str:
        return "'moleditpy_ok'; [System.Net.Dns]::GetHostName()"


POSIX = Dialect()
POWERSHELL = PowerShellDialect()


def powershell_encoded(command: str) -> str:
    """``command`` as a self-contained ``powershell -EncodedCommand`` line.

    This is what makes a Windows host reachable over SSH. Everything the plugin
    sends such a host is PowerShell, and OpenSSH on Windows hands a command to
    whatever the server has configured as its default shell -- which is cmd.exe
    unless somebody changed it, and cmd is not PowerShell. Sending the source
    through cmd (or through a POSIX shell on the way out) mangles it: quotes,
    ``$``, ``&`` and ``|`` all mean something to those shells too.

    UTF-16LE base64 has no such characters in it, so the command survives any
    number of shells between here and PowerShell, and works whether the far end
    defaults to cmd or to PowerShell.
    """
    encoded = base64.b64encode((command or "").encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"


def wrap_remote(host, command: str) -> str:
    """What actually goes down the wire to ``host`` for ``command``.

    Only a Windows host is wrapped, and only over a network: the local backend
    starts PowerShell itself, so there is nothing in between to protect it from.
    """
    from .models import BACKEND_LOCAL, BACKEND_WSL, SCHEDULER_WINDOWS

    if getattr(host, "scheduler", "") != SCHEDULER_WINDOWS:
        return command
    if getattr(host, "backend", "") in (BACKEND_LOCAL, BACKEND_WSL):
        return command
    return powershell_encoded(command)


def for_host(host) -> Dialect:
    """The dialect this host's commands are written in."""
    from .models import SCHEDULER_WINDOWS

    return POWERSHELL if getattr(host, "scheduler", "") == SCHEDULER_WINDOWS else POSIX


__all__ = [
    "MISSING",
    "PRESENT",
    "POSIX",
    "POWERSHELL",
    "Dialect",
    "PowerShellDialect",
    "for_host",
    "powershell_encoded",
    "wrap_remote",
]
