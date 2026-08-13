"""POSIX-side path and quoting helpers.

Used for every backend whose remote end is a POSIX shell, which is all of them
bar the native Windows one -- that has its own quoting in :mod:`dialect`, and
picking between the two is what :func:`dialect.for_host` is for. Paths are
built with ``posixpath`` and every interpolation goes through :func:`quote`.

``join`` is shared even on Windows: PowerShell accepts forward slashes in every
path it is given, so one path builder covers both rather than two that can
disagree about where a job directory is.
"""

from __future__ import annotations

import posixpath
import shlex
from typing import Iterable


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


def wrap_login(cmd: str, login_commands: Iterable[str]) -> str:
    """Prefix a command with the host's login/profile setup commands."""
    prefix = [c.strip() for c in (login_commands or []) if c and c.strip()]
    if not prefix:
        return cmd
    return "; ".join(prefix) + "; " + cmd
