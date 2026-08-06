"""Job Manager - submit calculations to remote clusters and track them.

Bridges the gap between the input generators and the result analyzers: upload
an input over SSH, submit it to SLURM/PBS/SGE (or plain nohup), poll the queue
on a deliberately slow timer, fetch the outputs when the job ends, and hand the
result to whichever plugin already claims that file type.

Job state lives in ``~/.moleditpy/job_manager/`` -- outside the plugin folder,
which the Plugin Installer replaces wholesale on update -- so tracked jobs
survive updates, restarts and "Reset All Settings".
"""

from __future__ import annotations

import logging
from typing import Any, Optional

PLUGIN_NAME = "Job Manager"
PLUGIN_VERSION = "0.1.0"
PLUGIN_AUTHOR = "HiroYokoyama"
PLUGIN_DESCRIPTION = (
    "Submit calculations to remote HPC clusters over SSH, track queue status, "
    "and fetch results back into MoleditPy."
)
PLUGIN_CATEGORY = "Utility"
PLUGIN_TAGS = ["hpc", "ssh", "job", "utility"]
# The default OpenSSH backend needs nothing beyond the host app; paramiko is an
# opt-in backend and is deliberately not forced on every user.
PLUGIN_DEPENDENCIES = []
PLUGIN_SUPPORTED_MOLEDITPY_VERSION = ">=4.0.0, <5.0.0"
PLUGIN_SUPPORTED_OS = ["Windows", "macOS", "Linux", "WSL"]

WINDOW_KEY = "job_monitor"

_context: Optional[Any] = None
_service: Optional[Any] = None


def get_context() -> Optional[Any]:
    """The PluginContext captured in :func:`initialize`."""
    return _context


def get_service(create: bool = True) -> Optional[Any]:
    """The session-scoped :class:`~job_manager.service.JobService`.

    Created lazily on first use so merely loading the plugin costs nothing and
    no network activity starts until the user opens the window.
    """
    global _service
    if _service is None and create:
        from .service import JobService

        _service = JobService()
    return _service


def forget_window() -> None:
    """Drop the registered window so the next open builds a live one.

    A dialog that stays registered after being closed comes back as a stale
    widget whose signals are already torn down.
    """
    if _context is not None:
        try:
            _context.register_window(WINDOW_KEY, None)
        except Exception:
            logging.debug("Job Manager: could not deregister the window", exc_info=True)


def initialize(context) -> None:
    """Entry point called by the host at plugin load."""
    global _context
    _context = context
    context.add_plugin_menu("Job Manager/Job Monitor", lambda: show_monitor(context))
    context.add_plugin_menu("Job Manager/Submit Job...", lambda: show_submit(context))


def run(mw) -> None:
    """Legacy Plugins-menu entry."""
    show_monitor(_context)


def show_monitor(context=None) -> None:
    """Open (or raise) the singleton job monitor."""
    context = context or _context
    if context is None:
        return
    window = context.get_window(WINDOW_KEY)
    if window is not None:
        window.show()
        window.raise_()
        window.activateWindow()
        return
    try:
        from .jobs_dialog import JobsDialog

        service = get_service()
        window = JobsDialog(service, context.get_main_window())
        context.register_window(WINDOW_KEY, window)
        window.show()
    except Exception as exc:
        logging.exception("Job Manager: could not open the job monitor")
        context.show_status_message(f"Job Manager: {exc}", 5000)


def show_submit(context=None) -> None:
    """Open the submit wizard, creating the monitor behind it."""
    context = context or _context
    if context is None:
        return
    show_monitor(context)
    window = context.get_window(WINDOW_KEY)
    if window is not None:
        window.open_submit_dialog()


def submit_file(paths, name: str = "") -> bool:
    """Open the submit wizard prefilled with an input file. **Public API.**

    This is the handoff other plugins use: an input generator that has just
    written a file calls it to offer "run this on the cluster" without knowing
    anything about hosts, schedulers or transports. Callers find this plugin
    through the host's plugin list and check for this attribute, so the name
    and signature are a contract -- do not rename either.

    ``paths`` is one path or a list of them; the first is the file passed to
    the command. Returns True if the wizard was opened.
    """
    if isinstance(paths, str):
        paths = [paths]
    files = [p for p in (paths or []) if p]
    if not files or _context is None:
        return False
    show_monitor(_context)
    window = _context.get_window(WINDOW_KEY)
    if window is None:
        return False
    try:
        window.open_submit_dialog(files=files, name=name)
    except Exception:
        logging.exception("Job Manager: could not open the submit wizard")
        return False
    return True


def shutdown() -> None:
    """Stop polling and release worker threads (called on plugin reload)."""
    global _service
    if _service is not None:
        try:
            _service.shutdown()
        except Exception:
            logging.debug("Job Manager: shutdown failed", exc_info=True)
        _service = None
