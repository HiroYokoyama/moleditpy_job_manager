"""POSIX-side path and quoting helpers.

The client may be Windows, but the remote end is always a POSIX shell, so every
path we build uses ``posixpath`` and every interpolation goes through
:func:`quote`.
"""

from __future__ import annotations

import posixpath
import shlex
from typing import Iterable, List


def quote(path: str) -> str:
    """Shell-quote a remote path while keeping a leading ``~`` expandable.

    ``shlex.quote("~/jobs")`` yields ``'~/jobs'``, which the remote shell treats
    as a literal directory named ``~``. Quoting only the part after the tilde
    keeps expansion working.
    """
    text = path or ""
    if text == "~":
        return "~"
    if text.startswith("~/"):
        rest = text[2:]
        return "~/" + shlex.quote(rest) if rest else "~/"
    return shlex.quote(text)


def join(*parts: str) -> str:
    """posixpath.join with empty segments dropped."""
    return posixpath.join(*[p for p in parts if p])


def basename(path: str) -> str:
    return posixpath.basename(path)


def dirname(path: str) -> str:
    return posixpath.dirname(path)


def build_command(commands: Iterable[str]) -> str:
    """Chain shell commands so the first failure aborts the rest."""
    parts: List[str] = [c.strip() for c in commands if c and c.strip()]
    return " && ".join(parts)


def wrap_login(cmd: str, login_commands: Iterable[str]) -> str:
    """Prefix a command with the host's login/profile setup commands."""
    prefix = [c.strip() for c in (login_commands or []) if c and c.strip()]
    if not prefix:
        return cmd
    return "; ".join(prefix) + "; " + cmd
