"""Asking for a host password, on the GUI thread, once per session.

Nothing here writes a secret anywhere: the answer goes to
:meth:`JobService.set_password`, which keeps it in a plain dict for the life of
the process. It is never persisted, never logged and never put on a command
line -- the OpenSSH backend runs in batch mode precisely so that no password
can reach ``ps``.

The prompt has to happen before any worker is dispatched, since a background
thread cannot open a dialog. Polling therefore never prompts: an uncached host
simply fails its poll and backs off until the user does something interactive.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QInputDialog, QLineEdit, QWidget

from .models import BACKEND_PARAMIKO, HostProfile


def needs_password(service, host: HostProfile) -> bool:
    """True when this host is configured to ask and has no answer yet."""
    if host is None or host.backend != BACKEND_PARAMIKO or not host.ask_password:
        return False
    return not service.has_password(host.id)


def ensure_password(service, host: HostProfile, parent: Optional[QWidget] = None) -> bool:
    """Prompt if required. False means the user cancelled; do not proceed."""
    if not needs_password(service, host):
        return True
    password, accepted = QInputDialog.getText(
        parent,
        "Job Manager",
        f"Password for {host.target}:\n(kept in memory for this session only)",
        QLineEdit.EchoMode.Password,
    )
    if not accepted:
        return False
    service.set_password(host.id, password)
    return True


__all__ = ["ensure_password", "needs_password"]
