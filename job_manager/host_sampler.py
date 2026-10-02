"""Load and memory for every host, sampled for whoever is looking.

One sampler per service, shared by the Host Monitor window and the web view.
Each of them holds it while it is looking -- the window while it is open, the
web view for a while after each request -- and sampling stops the moment the
last one lets go, so a Job Manager left running overnight touches no login
node. Two lookers never mean two probes: a host is asked once per interval
whoever wants the answer.

The transport is held open per host while sampling runs: rebuilding a
connection every two seconds would cost more than the measurement.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import host_stats
from .credentials import needs_password
from .models import SCHEDULER_WINDOWS, HostProfile
from .tasks import run_async

#: Two seconds suits a backend that keeps its connection (paramiko, local).
#: OpenSSH spawns a fresh ssh process per command -- a fresh TCP connect,
#: handshake and auth every tick -- and a burst of them trips sshd's own
#: connection throttling, which shows up here as a timeout on a healthy host.
DEFAULT_INTERVAL_SECONDS = 2
OPENSSH_INTERVAL_SECONDS = 10

#: A host that fails is asked less often, doubling up to this, rather than
#: every tick for as long as anyone is looking.
MAX_BACKOFF_TICKS = 16


class HostSampler(QObject):
    """Asks each sampled host for its load, on a timer, while held."""

    #: A host answered: ``(host_id, HostStats)``.
    sampled = pyqtSignal(str, object)
    #: A host did not: ``(host_id, message, seconds until it is asked again)``.
    #: Zero seconds when no connection could even be built.
    sample_failed = pyqtSignal(str, str, int)
    #: Every tick, before any host is asked: the window re-reads the host list.
    ticking = pyqtSignal()

    def __init__(self, service) -> None:
        super().__init__()
        self.service = service
        #: Who is holding it. Sampling runs while this is not empty.
        self._holders: set = set()
        self._transports: Dict[str, object] = {}
        #: Hosts with a probe still in flight, so a slow host does not queue
        #: up one worker per tick.
        self._busy: set = set()
        #: Ticks still to skip for a host that failed, and the size of the
        #: skip it earned. Both cleared by a sample that works.
        self._skip_ticks: Dict[str, int] = {}
        self._backoff: Dict[str, int] = {}
        #: The last sample per host, failed ones included, for a looker that
        #: arrives after it was taken.
        self._latest: Dict[str, host_stats.HostStats] = {}
        #: When each host was last asked, so one with its own interval is
        #: sampled on that and not on every tick.
        self._last_sample: Dict[str, float] = {}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.sample_all)

    # --- who is looking -------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self._holders)

    def acquire(self, holder: object) -> None:
        """Start sampling, or join it. The first holder gets a sample at once
        rather than one interval later."""
        first = not self._holders
        self._holders.add(id(holder))
        if first:
            self._timer.start(self.tick_seconds() * 1000)
            self.sample_all()

    def release(self, holder: object) -> None:
        """Let go. The last holder to leave stops sampling and hands every
        connection back."""
        self._holders.discard(id(holder))
        if not self._holders:
            self.stop()

    def stop(self) -> None:
        self._holders.clear()
        self._timer.stop()
        for host_id in list(self._transports):
            self.close_transport(host_id)

    # --- cadence ---------------------------------------------------------------

    def default_interval(self) -> int:
        """The cadence the slowest backend in the list can stand."""
        from .models import BACKEND_OPENSSH

        hosts = list(self.service.store.host_list())
        if any(host.backend == BACKEND_OPENSSH for host in hosts):
            return OPENSSH_INTERVAL_SECONDS
        return DEFAULT_INTERVAL_SECONDS

    def interval_seconds(self) -> int:
        """The stored choice wins; the per-backend default is only a starting
        point, not a correction applied over the top of it."""
        stored = int(self.service.store.get_pref("host_monitor_interval", 0) or 0)
        return max(1, stored or self.default_interval())

    def interval_for(self, host: HostProfile) -> int:
        """Seconds between samples of one host: its own, else the shared one."""
        own = int(getattr(host, "monitor_interval", 0) or 0)
        return own if own > 0 else self.interval_seconds()

    def tick_seconds(self) -> int:
        """The timer runs as often as the most eager sampled host wants."""
        seconds = [
            self.interval_for(host)
            for host in self.service.store.host_list()
            if getattr(host, "enabled", True) and getattr(host, "monitor_usage", True)
        ]
        return max(1, min(seconds or [self.interval_seconds()]))

    def reschedule(self) -> None:
        """The interval or the host list changed."""
        if self._timer.isActive():
            self._timer.setInterval(self.tick_seconds() * 1000)

    # --- sampling ----------------------------------------------------------------

    def hosts(self) -> List[HostProfile]:
        return [
            host
            for host in self.service.store.host_list()
            if getattr(host, "enabled", True)
            and getattr(host, "monitor_usage", True)
            and not needs_password(self.service, host)
        ]

    def latest(self, host_id: str) -> Optional[host_stats.HostStats]:
        return self._latest.get(host_id)

    def _transport_for(self, host: HostProfile):
        transport = self._transports.get(host.id)
        if transport is None:
            transport = self.service.transport_for(host)
            self._transports[host.id] = transport
        return transport

    def sample_all(self) -> None:
        self.ticking.emit()
        now = time.monotonic()
        tick = self.tick_seconds()
        for host in self.hosts():
            # Only a host slower than the tick is gated on the clock; the rest
            # are asked every tick. Half a tick of slack, so a timer firing a
            # hair early does not skip a whole interval.
            last = self._last_sample.get(host.id)
            interval = self.interval_for(host)
            if interval > tick and last is not None and now - last + tick / 2.0 < interval:
                continue
            if host.id in self._busy:
                # Still waiting on the last probe; stacking more would only
                # make a slow host slower.
                continue
            waiting = self._skip_ticks.get(host.id, 0)
            if waiting:
                # Backing off after a failure, so as not to hammer an
                # unreachable host every tick.
                self._skip_ticks[host.id] = waiting - 1
                continue
            self._last_sample[host.id] = now
            self._sample(host)

    def _sample(self, host: HostProfile) -> None:
        self._busy.add(host.id)
        command = host_stats.command_for(host.scheduler == SCHEDULER_WINDOWS)
        host_id = host.id
        # Resolved on the GUI thread: concurrent workers reading/writing
        # self._transports without a lock would be a data race.
        try:
            transport = self._transport_for(host)
        except Exception as exc:
            self._busy.discard(host_id)
            self.sample_failed.emit(host_id, str(exc), 0)
            return

        def work() -> str:
            result = transport.run(command, timeout=max(15, int(host.connect_timeout or 10)))
            return result.stdout

        def ok(text: str) -> None:
            self._busy.discard(host_id)
            self._backoff.pop(host_id, None)
            self._skip_ticks.pop(host_id, None)
            parsed = host_stats.parse(text)
            self._latest[host_id] = parsed
            self.sampled.emit(host_id, parsed)

        def failed(message: str) -> None:
            self._busy.discard(host_id)
            # A failed probe drops the connection, so the next tick builds a
            # new one instead of reusing an already-closed socket.
            self.close_transport(host_id)
            waited = min(MAX_BACKOFF_TICKS, max(1, self._backoff.get(host_id, 0) * 2 or 1))
            self._backoff[host_id] = waited
            self._skip_ticks[host_id] = waited
            self._latest[host_id] = host_stats.HostStats(error=message)
            self.sample_failed.emit(host_id, message, waited * self.interval_for(host))

        run_async(self.service.pool, work, on_success=ok, on_error=failed, quiet=True)

    def close_transport(self, host_id: str) -> None:
        """Hand a transport's teardown to the pool instead of closing it here:
        paramiko's close() can block on a host that has gone quiet. Popped
        from ``self._transports`` immediately either way, so a probe stops
        seeing it as open the moment this returns."""
        transport = self._transports.pop(host_id, None)
        if transport is None:
            return

        def close() -> None:
            try:
                transport.close()
            except Exception:  # pragma: no cover - closing must never raise here
                pass

        run_async(self.service.pool, close, quiet=True)


def for_service(service) -> HostSampler:
    """The one sampler for this service, made on first use."""
    sampler = getattr(service, "_host_sampler", None)
    if sampler is None:
        sampler = HostSampler(service)
        service._host_sampler = sampler
    return sampler


def shutdown_for(service) -> None:
    sampler = getattr(service, "_host_sampler", None)
    if sampler is not None:
        sampler.stop()


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "MAX_BACKOFF_TICKS",
    "OPENSSH_INTERVAL_SECONDS",
    "HostSampler",
    "for_service",
    "shutdown_for",
]
