"""The web view's life: its socket, its snapshot, and when to sample for it.

Owned per service rather than by the Host Monitor window. It used to live in
the window, so closing the window took the socket with it and a phone's
saved link loaded a blank page until someone at the desk opened it again.

It serves whenever the Settings switch is on. Sampling is another matter: it
costs every login node a command per interval, so it runs only while someone
is looking -- a request holds the shared sampler for :data:`IDLE_SECONDS`, and
each further request extends that.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import host_sampler
from .models import TERMINAL_STATES

#: How long a request keeps the hosts sampled. Longer than the page's slowest
#: refresh (five minutes), so a tab left on that setting sees fresh numbers
#: rather than starting the sampler only to find it stopped by the next poll.
IDLE_SECONDS = 360

#: How long a start waits for the port to come free before taking another.
#: The tray process starts while the MoleditPy it replaces is still closing,
#: holding the port the saved links name.
PORT_WAIT_SECONDS = 15


class WebService(QObject):
    """Starts and stops the web view, and keeps its snapshot current."""

    #: Emitted from the HTTP thread; Qt queues it onto this object's thread.
    _requested = pyqtSignal()
    #: The server started or stopped, for a Settings window that is open.
    changed = pyqtSignal()

    def __init__(self, service) -> None:
        super().__init__()
        self.service = service
        self.sampler = host_sampler.for_service(service)
        self.server = None
        #: Why the last start failed, for whoever asked.
        self.error = ""
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._stop_sampling)
        self._waiting = QTimer(self)
        self._waiting.setSingleShot(True)
        self._waiting.timeout.connect(self._try_exact_port)
        self._wait_until = 0.0
        self._holding = False
        self._requested.connect(self._on_request)
        self.sampler.sampled.connect(self.publish)
        self.sampler.sample_failed.connect(self.publish)
        service.jobs_changed.connect(self.publish)

    # --- state ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.server is not None and self.server.running

    @property
    def port(self) -> int:
        return self.server.port if self.running else 0

    def _preferred_port(self) -> int:
        from .web_monitor import DEFAULT_PORT

        return int(self.service.store.get_pref("host_monitor_web_port", DEFAULT_PORT))

    # --- on and off ---------------------------------------------------------------

    def start(self, wait_for_port: bool = False) -> bool:
        """Serve, and remember that the user wants it served.

        ``wait_for_port`` keeps trying the saved port for a while before
        settling for another, for a process that knows the previous owner is
        on its way out.
        """
        from .web_monitor import WebMonitorServer, ensure_web_token

        self.error = ""
        if self.running:
            return True
        # Read from disk, not minted here: a link saved on a phone has to keep
        # working across restarts.
        self.server = WebMonitorServer(
            ensure_web_token(self.service.store.directory), on_request=self._requested.emit
        )
        self.service.store.set_pref("host_monitor_web", True)
        if wait_for_port:
            self._wait_until = time.monotonic() + PORT_WAIT_SECONDS
            return self._try_exact_port()
        try:
            self.server.start(self._preferred_port())
        except OSError as exc:
            self.server = None
            self.error = str(exc)
            logging.warning("Job Manager: web monitor did not start: %s", exc)
            self.changed.emit()
            return False
        self.publish()
        self.changed.emit()
        return True

    def _try_exact_port(self) -> bool:
        if self.server is None:
            return False
        try:
            self.server.start(self._preferred_port(), exact=time.monotonic() < self._wait_until)
        except OSError as exc:
            if time.monotonic() < self._wait_until:
                self._waiting.start(500)
                return True
            self.server = None
            self.error = str(exc)
            logging.warning("Job Manager: web monitor did not start: %s", exc)
            self.changed.emit()
            return False
        self.publish()
        self.changed.emit()
        return True

    def stop(self, remember: bool = True) -> None:
        """Stop serving. ``remember=False`` is a process ending, not the user
        switching it off: the choice stays for the next start."""
        self._waiting.stop()
        if self.server is not None:
            self.server.stop()
        self.server = None
        self._stop_sampling()
        if remember:
            self.service.store.set_pref("host_monitor_web", False)
        self.changed.emit()

    def renew_token(self) -> str:
        """Mint a new secret, cutting off every link and cookie already out."""
        from .web_monitor import ensure_web_token

        token = ensure_web_token(self.service.store.directory, renew=True)
        if self.server is not None:
            self.server.set_token(token)
        return token

    # --- someone is looking --------------------------------------------------------

    def _on_request(self) -> None:
        if not self.running:
            return
        if not self._holding:
            self._holding = True
            self.sampler.acquire(self)
        self._idle.start(IDLE_SECONDS * 1000)

    def _stop_sampling(self) -> None:
        self._idle.stop()
        if self._holding:
            self._holding = False
            self.sampler.release(self)

    # --- what is served -------------------------------------------------------------

    def publish(self, *_args) -> None:
        if self.running:
            self.server.publish(self.snapshot())

    def snapshot(self) -> dict:
        """Everything the page shows, as plain data.

        Built on the GUI thread and handed over finished. The HTTP thread gets
        a dict and never a transport or a store cursor -- reading either of
        those from a request handler is the bug this shape exists to prevent.
        """
        from .host_monitor import NOT_SAMPLED, primary_state_word

        by_host: dict = {}
        for job in self.service.store.job_list():
            # Excluding TERMINAL_STATES, not "not in ACTIVE_STATES": that set
            # is about which jobs the poller must still contact the host for,
            # and leaves out DOWNLOADING, QUEUED and BLOCKED -- all of which
            # are exactly what someone opens this page to look at.
            if job.state in TERMINAL_STATES:
                continue
            by_host.setdefault(job.host_id, []).append(
                {"name": job.name, "state": primary_state_word(job)}
            )

        hosts = []
        for host in self.service.store.host_list():
            sampled = host.monitor_usage
            # A value left from before sampling was switched off would be
            # served as if it were current.
            stats = self.sampler.latest(host.id) if sampled else None
            entry = {
                "name": host.name,
                "jobs": by_host.get(host.id, []),
                "summary": "",
                "error": "",
                "load_fraction": 0.0,
                "memory_fraction": 0.0,
                "load_detail": "",
                "memory_detail": "",
            }
            if not sampled:
                entry["summary"] = NOT_SAMPLED
            elif not host.enabled:
                entry["summary"] = "disabled"
            elif stats is None:
                # Sampling starts with the first request, so the first load
                # names the hosts and says why there are no numbers yet.
                entry["summary"] = "waiting for the first reading..."
            else:
                entry["summary"] = stats.summary
                entry["error"] = stats.error
                entry["load_fraction"] = stats.load_fraction
                entry["memory_fraction"] = stats.memory_fraction
                if stats.load:
                    entry["load_detail"] = f"{stats.load[0]:.2f}"
                if stats.mem_total_mb and stats.mem_free_mb:
                    entry["memory_detail"] = (
                        f"{stats.mem_used_mb / 1024:.1f}/{stats.mem_total_mb / 1024:.1f} GB"
                    )
            hosts.append(entry)
        return {"hosts": hosts, "generated": time.strftime("%H:%M:%S")}


def for_service(service, create: bool = True) -> Optional[WebService]:
    """The one web view for this service."""
    web = getattr(service, "_web_service", None)
    if web is None and create:
        web = WebService(service)
        service._web_service = web
    return web


def resume(service, wait_for_port: bool = False) -> None:
    """Serve at start-up when the user left it on."""
    if service.store.get_pref("host_monitor_web", False):
        for_service(service).start(wait_for_port=wait_for_port)


def shutdown_for(service) -> None:
    """The process or the plugin load is ending; the choice is kept."""
    web = for_service(service, create=False)
    if web is not None:
        web.stop(remember=False)
    host_sampler.shutdown_for(service)


__all__ = [
    "IDLE_SECONDS",
    "PORT_WAIT_SECONDS",
    "WebService",
    "for_service",
    "resume",
    "shutdown_for",
]
