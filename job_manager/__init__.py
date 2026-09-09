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
PLUGIN_VERSION = "1.6.1"
PLUGIN_AUTHOR = "HiroYokoyama"

PLUGIN_DESCRIPTION = (
    "Submit calculations to remote HPC clusters over SSH, track queue status, "
    "and fetch results back into MoleditPy. Drop an input file on the monitor "
    "and the wizard opens prefilled, reading the memory and core request "
    "straight out of the ORCA, Gaussian, Psi4, NWChem, Q-Chem or GAMESS input; "
    "results come back next to it and a notification says when. Work already "
    "staged on the cluster is submitted where it sits, with no input file to "
    "upload at all. Runs on this "
    "machine too, with no SSH -- natively on Windows through PowerShell, with "
    "nothing to install. Installing paramiko adds a backend that keeps one SSH "
    "session open -- which the live host panel samples through -- and that can "
    "log in with a password where a key is not an option. On a machine with no "
    "scheduler it keeps a small queue "
    "of its own that schedules on physical cores and memory, so two large jobs "
    "never share a machine that cannot hold both, chains jobs with each "
    "scheduler's own dependency flag, holds a job until a chosen time, and "
    "outlives MoleditPy. Other programs on the same machine can submit and "
    "track jobs through a local HTTP API, off until you switch it on."
)
PLUGIN_CATEGORY = "Utility"
PLUGIN_TAGS = ["hpc", "ssh", "job", "Utility"]
# The default OpenSSH backend needs nothing beyond the host app, so nothing
# here is required to submit a job.
PLUGIN_DEPENDENCIES = []
#: Unlocks the paramiko backend. It authenticates with a key, an agent or a
#: password -- not passwords only -- and it is the one backend that keeps a
#: single SSH session open rather than starting a new ssh process per command,
#: which is what makes the live host panel cheap to sample.
#: Optional rather than required because the default OpenSSH backend covers
#: key and agent authentication with nothing installed, and the Plugin
#: Installer lists an optional dependency without ever blocking an install.
PLUGIN_OPTIONAL_DEPENDENCIES = ["paramiko"]
PLUGIN_SUPPORTED_MOLEDITPY_VERSION = ">=4.0.0, <5.0.0"
PLUGIN_SUPPORTED_OS = ["Windows", "macOS", "Linux", "WSL"]

WINDOW_KEY = "job_monitor"
#: Registered separately from WINDOW_KEY so opening it standalone (Extensions >
#: Job Manager > Host Monitor) never has to build the job monitor first.
HOST_MONITOR_WINDOW_KEY = "job_manager_host_monitor"

_context: Optional[Any] = None
_service: Optional[Any] = None
_status_widget: Optional[Any] = None
_api_server: Optional[Any] = None


def get_context() -> Optional[Any]:
    """The PluginContext captured in :func:`initialize`."""
    return _context


def get_service(create: bool = True, store: Optional[Any] = None) -> Optional[Any]:
    """The session-scoped :class:`~job_manager.service.JobService`.

    Created lazily on first use so merely loading the plugin costs nothing and
    no network activity starts until there is a reason for it. ``store`` adopts
    an already-loaded :class:`~job_manager.store.JobStore` rather than reading
    the same files a second time.
    """
    global _service
    if _service is None and create:
        from .service import JobService

        _service = JobService(store=store)
        # Before the status widget: a host with no status bar still gets told
        # when its jobs end.
        _service.job_finished.connect(_notify_finished)
        _install_status_widget(_service)
    return _service


def get_api_server(create: bool = True) -> Optional[Any]:
    """The local API server object, whether or not it is listening.

    Building one starts nothing: it owns a socket only after :func:`start_api`.
    """
    global _api_server
    if _api_server is None and create:
        from .api_server import JobApiServer

        _api_server = JobApiServer(get_service())
    return _api_server


def start_api(port: int = 0) -> int:
    """Start listening on 127.0.0.1. Returns the port, or 0 if it failed.

    Never called on its own initiative -- only from the preference being on at
    load, or the user switching it on. See :func:`_resume_api`.
    """
    server = get_api_server()
    if server is None:
        return 0
    if server.running:
        return server.port
    try:
        return server.start(port or int(_service.store.get_pref("api_port", 0) or 0))
    except Exception as exc:
        logging.exception("Job Manager: the local API could not start")
        if _context is not None:
            _context.show_status_message(f"Job Manager API: {exc}", 5000)
        return 0


def stop_api() -> None:
    """Stop listening, and take the endpoint file with it."""
    if _api_server is not None:
        try:
            _api_server.stop()
        except Exception:
            logging.debug("Job Manager: the local API did not stop cleanly", exc_info=True)


def api_is_running() -> bool:
    return bool(_api_server is not None and _api_server.running)


def _resume_api(store: Optional[Any] = None) -> None:
    """Start the API at load when the user has switched it on before.

    Takes the store the startup peek already read rather than reading it
    again: two JobStores at launch parse both files twice and leave two views
    of the same jobs. A session with the API off still builds no service.
    """
    store = store if store is not None else (_service.store if _service is not None else None)
    if store is None or not store.get_pref("api_enabled", False):
        return
    get_service(store=store)
    start_api()


def _finished_words() -> dict:
    """What each terminal state is called in a notification.

    Read at a glance with no monitor open to give it context, so "failed"
    rather than "FAILED". Keyed by the canonical constants, not by literals: a
    state that was renamed would otherwise silently fall back to its own name.
    """
    from .models import STATE_CANCELLED, STATE_DONE, STATE_FAILED, STATE_LOST

    return {
        STATE_DONE: "finished",
        STATE_FAILED: "failed",
        STATE_CANCELLED: "was cancelled",
        STATE_LOST: "disappeared from the queue",
    }


def _notify_finished(job_id: str, state: str) -> None:
    """Say that a job ended: on this desktop, and in a chat room if asked."""
    if _service is None or not _service.store.get_pref("notify_on_finish", True):
        return
    job = _service.store.jobs.get(job_id)
    if job is None:
        return
    wording = _finished_words().get(state, state.lower())
    message = f"{job.name} {wording} on {job.host_name}."
    try:
        from . import notify

        notify.notify("MoleditPy job manager", message)
    except Exception:
        logging.debug("Job Manager: could not raise a notification", exc_info=True)
    # Separately, and after: a chat room is the notification for the case where
    # nobody is at this desktop to see the other one, so a tray that refuses the
    # message must not take this with it.
    try:
        from . import webhook

        url = str(_service.store.get_pref("notify_webhook", "") or "")
        if _service.store.get_pref("notify_chat", False):
            webhook.post_async(url, "MoleditPy job manager", message)
    except Exception:
        logging.debug("Job Manager: could not post to the chat webhook", exc_info=True)


def _install_status_widget(service) -> None:
    """Put the job counter in the host's status bar, once."""
    global _status_widget
    if _status_widget is not None or _context is None:
        return
    try:
        from .status_widget import install

        _status_widget = install(
            _context.get_main_window(), service, on_click=lambda: show_monitor(_context)
        )
    except Exception:
        # A missing status bar, or a host that lays its own out differently, is
        # not a reason to leave the user without a working plugin.
        logging.debug("Job Manager: no status bar indicator", exc_info=True)


def _startup_store() -> Optional[Any]:
    """The job list, read once at load for everything that has to peek at it.

    Reading it costs nothing that would not be paid anyway, and an empty list
    still means not a single byte of network traffic.
    """
    try:
        from .store import JobStore

        return JobStore()
    except Exception:
        logging.debug("Job Manager: could not read the job list at startup", exc_info=True)
        return None


def _resume_tracking(store: Optional[Any] = None) -> None:
    """Start polling at launch when jobs from a previous session are running.

    The service used to be built only by opening the monitor, so a restart with
    three jobs on a cluster silently stopped tracking every one of them: no
    polling, no auto-download, until the user happened to open the window. The
    store is read either way, so peeking at it first costs nothing -- and an
    empty job list still means not a single byte of network traffic.
    """
    if _service is not None or store is None or not store.active_jobs():
        return
    # The store just read is the one the service adopts, rather than parsing
    # both files again a line later.
    get_service(store=store)


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


def open_job_file(path: str) -> None:
    """Open a saved job list in the monitor. Registered with the host.

    Makes ``.pmejbs`` a file type the application knows: File > Import, the
    command line and a drop onto the main window all land here.
    """
    show_monitor(_context)
    window = _context.get_window(WINDOW_KEY) if _context is not None else None
    if window is not None:
        window.open_job_list(path)


def handle_dropped_file(path: str) -> bool:
    """Open a job list dropped onto the main window. True when it was ours.

    Only ``.pmejbs``. Input extensions are deliberately *not* claimed
    application-wide: taking ``.inp`` and ``.xyz`` would stop a drop on the
    main window doing the obvious thing, which is opening the molecule. The
    monitor and the wizard accept those themselves, where the meaning of a drop
    is unambiguous.
    """
    from .store import JOB_EXTENSION

    if not path or not path.lower().endswith(JOB_EXTENSION):
        return False
    try:
        open_job_file(path)
    except Exception:
        logging.exception("Job Manager: could not open the dropped job list %s", path)
        return False
    return True


def initialize(context) -> None:
    """Entry point called by the host at plugin load."""
    global _context
    _context = context
    # Extensions rather than the Plugin menu. The host has no Extensions menu
    # of its own and creates it on demand, so this is a top-level entry.
    # add_menu_action, not add_plugin_menu: the latter is hard-wired to
    # "Plugin/<path>". Both have existed since v3, so no fallback is needed.
    context.add_menu_action("Extensions/Job Manager/Job Monitor", lambda: show_monitor(context))
    # Standalone: opens only the host panel, not the job monitor behind it --
    # for the case this window is built for, watching machines with nothing
    # queued yet, where building the job monitor first was wasted work (and a
    # second window to close).
    context.add_menu_action(
        "Extensions/Job Manager/Host Monitor", lambda: show_host_monitor_standalone(context)
    )
    context.add_menu_action("Extensions/Job Manager/Submit Job...", lambda: show_submit(context))
    # Its own entry rather than a tick in the monitor's preferences row: this
    # is where the token is read from, and a user following the API
    # documentation should not have to open a job window to find it.
    context.add_menu_action("Extensions/Job Manager/Local API...", lambda: show_api_dialog(context))
    # Last, and the only entry that reaches nothing on a host: which version is
    # running is what a bug report needs, and once the plugin is installed the
    # Installer's listing is no longer in front of anyone.
    context.add_menu_action("Extensions/Job Manager/About...", lambda: show_about(context))

    from .store import JOB_EXTENSION

    try:
        context.register_file_opener(JOB_EXTENSION, open_job_file)
    except AttributeError:
        # Host older than the file-opener API; the menu entries still work.
        logging.debug("Job Manager: this host has no register_file_opener")

    try:
        # Priority 0: this handler answers for one extension and declines
        # everything else, so it has no reason to get in front of a plugin
        # that wants a say in the file types it does claim.
        context.register_drop_handler(handle_dropped_file, 0)
    except AttributeError:
        logging.debug("Job Manager: this host has no register_drop_handler")

    # One store for both peeks: each used to build its own, which parses both
    # files twice at every launch.
    store = _startup_store()
    _resume_tracking(store)
    # After tracking, so an API that is on adopts the service that resume
    # already built rather than making a second one.
    _resume_api(store)


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
        window = JobsDialog(service, parent=None)
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


def show_host_monitor(context=None) -> None:
    """Open the host monitor, creating the main monitor behind it."""
    context = context or _context
    if context is None:
        return
    show_monitor(context)
    window = context.get_window(WINDOW_KEY)
    if window is not None:
        window.open_host_monitor()


def show_host_monitor_standalone(context=None) -> None:
    """Open (or raise) the host monitor on its own -- no job monitor window.

    The job monitor is a QDialog that stays fully independent (see
    window_utils.make_independent); the host monitor already was one too, but
    every route to it went through show_monitor() first, so the Extensions
    menu could not open one without also raising the other. This registers
    the host monitor under its own window key so it is a standalone window
    like the job monitor is, not a side effect of opening it.
    """
    context = context or _context
    if context is None:
        return
    window = context.get_window(HOST_MONITOR_WINDOW_KEY)
    if window is not None:
        window.show()
        window.raise_()
        window.activateWindow()
        return
    try:
        from .host_monitor import HostMonitorDialog

        service = get_service()
        window = HostMonitorDialog(service, parent=None)
        context.register_window(HOST_MONITOR_WINDOW_KEY, window)
        window.finished.connect(lambda *_: context.register_window(HOST_MONITOR_WINDOW_KEY, None))
        window.show()
    except Exception as exc:
        logging.exception("Job Manager: could not open the host monitor")
        context.show_status_message(f"Job Manager: {exc}", 5000)


def submit_file(paths, name: str = "") -> bool:
    """Open the submit wizard prefilled with an input file. **Public API.**

    This is the handoff other plugins use: an input generator that has just
    written a file calls it to offer "run this on the cluster" without knowing
    anything about hosts, schedulers or transports. Callers find this plugin
    through the host's plugin list and check for this attribute, so the name
    and signature are a contract -- do not rename either.

    ``paths`` is one path or a list of them; the first is the file passed to
    the command. Returns True if the wizard was opened.

    The wizard opens on a host with an equal path (local mirror) configured
    where there is one, rather than on whichever host was used last: work
    arriving this way has no host in mind, and a mirrored host is the one
    whose results need no downloading.
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
        window.open_submit_dialog(files=files, name=name, handoff=True)
    except Exception:
        logging.exception("Job Manager: could not open the submit wizard")
        return False
    return True


def show_api_dialog(context=None) -> None:
    """Open the Local API window: the switch, the port, and the token."""
    context = context or _context
    if context is None:
        return
    try:
        from .api_dialog import ApiDialog

        ApiDialog(get_service(), parent=None).exec()
    except Exception as exc:
        logging.exception("Job Manager: could not open the API window")
        context.show_status_message(f"Job Manager: {exc}", 5000)


def show_about(context=None) -> None:
    """Open the About window: name, version, and where to report a problem."""
    context = context or _context
    try:
        from .about_dialog import AboutDialog

        AboutDialog(parent=None).exec()
    except Exception as exc:
        logging.exception("Job Manager: could not open the About window")
        if context is not None:
            context.show_status_message(f"Job Manager: {exc}", 5000)


def submit_job(request: dict) -> dict:
    """Submit a job from a dict, with no wizard and no user interaction.

    **Public API**, and the same one the HTTP route serves -- a plugin running
    inside MoleditPy calls this instead of going out over a socket to reach the
    process it is already in. ``request`` takes the fields documented in
    docs/API.md (``host`` plus ``command`` or ``preset``, ``files``, ...);
    the job record comes back as a dict.

    Raises :class:`~job_manager.api_core.ApiError` for a request that cannot be
    served, whose ``message`` is written to be shown to a user as it is.

    Must be called on the GUI thread, like every other writer of the job store.
    """
    from .api_core import JobApi

    return JobApi(get_service()).submit(request)["job"]


def shutdown() -> None:
    """Stop polling and release worker threads (called on plugin reload)."""
    global _service, _status_widget, _api_server
    # First: the socket is the one thing that can still bring in work, and a
    # request arriving while the service is being torn down would find half a
    # plugin. Unconditional, since a listening server outlives a reload.
    stop_api()
    _api_server = None
    if _status_widget is not None:
        try:
            _status_widget.detach()
        except Exception:
            logging.debug("Job Manager: status widget teardown failed", exc_info=True)
        _status_widget = None
    if _service is not None:
        try:
            _service.shutdown()
        except Exception:
            logging.debug("Job Manager: shutdown failed", exc_info=True)
        _service = None
        # Unconditionally, not only via the widget: a host with no status bar
        # never gets one, and a badge that outlives the plugin would leave
        # MoleditPy's icon claiming jobs are running for the rest of the run.
        try:
            from .taskbar import clear_badge

            clear_badge()
        except Exception:
            logging.debug("Job Manager: the badge was not cleared", exc_info=True)
        # Same reasoning for the tray icon: one left behind outlives the plugin
        # that put it there, and clicking it would reach nothing.
        try:
            from . import notify

            notify.shutdown()
        except Exception:
            logging.debug("Job Manager: the tray icon was not removed", exc_info=True)
