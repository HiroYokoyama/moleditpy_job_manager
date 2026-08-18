"""Background execution: one timer, a thread pool, and per-host backoff.

Design rules, all of them deliberate:

* **One status call per host per cycle**, never one per job. A user with 40
  queued jobs still costs the login node a single ``squeue``.
* **Slow by default.** 120 s, floored at 30 s; a shared login node is not a
  status API. Errors back off exponentially to 15 minutes.
* **The timer stops when nothing is active** and restarts on the next submit.
* **In-flight guard per host**, so a slow poll can never stack up behind itself.
* Failures log at ``warning``: the host pops a modal dialog for every
  ``logging.error`` (app 4.3.0+), which from a timer slot would storm the user.

The GUI thread is the only writer of the job store. Workers return plain data
through signals; nothing touches Qt widgets off-thread.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from .models import ACTIVE_STATES, HostProfile, Job
from .runner import poll_host, poll_runner
from .store import JobStore
from .transport.base import TransportError

#: Ceiling for the error backoff, in seconds.
MAX_BACKOFF = 900
#: A manual refresh may not be spammed faster than this.
MANUAL_REFRESH_COOLDOWN = 10.0
#: How soon a host is asked after a job has just been handed to it. The full
#: interval is the right cadence for watching a queue for hours; it is the
#: wrong one for the thirty seconds after a submission, when the user is
#: watching to see their job start and the table says SUBMITTED either way.
#: One extra query per submission is a user action, not a poll loop.
FIRST_POLL_SECONDS = 6.0


class _WorkerSignals(QObject):
    # host_id, {job_id: state}, {job_id: rc}, {job_id: message}
    finished = pyqtSignal(str, dict, dict, dict)
    failed = pyqtSignal(str, str)  # host_id, message


class _PollTask(QRunnable):
    """Polls one host on a pool thread."""

    def __init__(self, host: HostProfile, jobs: List[Job], transport_factory: Callable) -> None:
        super().__init__()
        self.host = host
        self.jobs = jobs
        self.transport_factory = transport_factory
        self.signals = _WorkerSignals()

    def run(self) -> None:  # pragma: no cover - exercised via run_sync in tests
        self.run_sync()

    def run_sync(self) -> None:
        """The body, callable directly so tests need no thread pool."""
        transport = None
        try:
            transport = self.transport_factory(self.host)
            if self.host.uses_remote_runner:
                updates = poll_runner(transport, self.host, self.jobs)
            else:
                updates = poll_host(transport, self.host, self.jobs)
        except (TransportError, ValueError, OSError) as exc:
            self.signals.failed.emit(self.host.id, str(exc))
            return
        except Exception as exc:  # a backend may raise anything; never kill the pool
            logging.warning("Job Manager: unexpected poll error: %s", exc, exc_info=True)
            self.signals.failed.emit(self.host.id, str(exc))
            return
        finally:
            # Every other caller closes its transport in a finally; this one runs
            # on a timer, so skipping it leaked a ControlMaster socket directory
            # (and a persistent ssh process) or an open paramiko connection on
            # every single poll, for the life of the session.
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    logging.debug("Job Manager: poll transport close failed", exc_info=True)
        # ``poll_host`` records the sentinel's exit code on the job it is given,
        # and ``poll_runner`` records why a job the helper set aside never ran.
        # Those are this task's private copies, so both are carried back as data
        # and applied on the GUI thread with everything else -- the reason used
        # to be written onto the copy and thrown away with it, so the one
        # message explaining a job that never started reached nobody.
        exit_codes = {job.id: job.rc for job in self.jobs if job.rc is not None}
        errors = {job.id: job.last_error for job in self.jobs if job.last_error}
        self.signals.finished.emit(self.host.id, updates, exit_codes, errors)


class JobPoller(QObject):
    """Drives periodic status checks for every host with active jobs."""

    job_updated = pyqtSignal(str, str)  # job_id, new_state
    host_error = pyqtSignal(str, str)  # host_id, message
    poll_started = pyqtSignal(str)  # host_id
    poll_finished = pyqtSignal(str)  # host_id

    def __init__(
        self,
        store: JobStore,
        transport_factory: Callable,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.transport_factory = transport_factory
        self.pool = QThreadPool(self)
        # Polling is I/O bound and login nodes dislike bursts; two at a time.
        self.pool.setMaxThreadCount(2)
        self._in_flight: Dict[str, bool] = {}
        self._backoff: Dict[str, float] = {}
        self._next_poll: Dict[str, float] = {}
        self._last_manual = 0.0
        #: Fires once, soon after a submission; see :meth:`prime`. A member
        #: rather than QTimer.singleShot so it can be stopped at shutdown and
        #: cannot outlive this object.
        self._kickoff = QTimer(self)
        self._kickoff.setSingleShot(True)
        self._kickoff.timeout.connect(self._on_kickoff)
        self.timer = QTimer(self)
        self.timer.setSingleShot(False)
        self.timer.timeout.connect(self.tick)

    # --- lifecycle ----------------------------------------------------------

    def interval_ms(self) -> int:
        return int(self.store.poll_interval * 1000)

    def start(self) -> None:
        """Begin (or resume) polling if any job is active."""
        if not self.store.active_jobs():
            self.stop()
            return
        self.timer.setInterval(self.interval_ms())
        if not self.timer.isActive():
            self.timer.start()

    def stop(self) -> None:
        if self.timer.isActive():
            self.timer.stop()

    def reschedule(self) -> None:
        """Apply a changed interval preference without losing the timer."""
        was_active = self.timer.isActive()
        self.timer.setInterval(self.interval_ms())
        if was_active:
            self.timer.start()
        else:
            self.start()

    def prime(self, host_id: str = "") -> None:
        """Ask this host again shortly, whatever the interval says.

        Called when a job has just been submitted. Without it the first status
        query after a submission is a whole poll interval away -- two minutes
        by default -- so a job that started at once still read SUBMITTED for
        two minutes, and an empty list that has just been given its first job
        showed nothing happening at all.
        """
        if host_id:
            self._next_poll.pop(host_id, None)
            self._backoff.pop(host_id, None)
        self.start()
        if not self._kickoff.isActive():
            self._kickoff.start(int(FIRST_POLL_SECONDS * 1000))

    def _on_kickoff(self) -> None:
        if self.store.active_jobs():
            self.tick()

    def shutdown(self) -> None:
        self._kickoff.stop()
        self.stop()
        self.pool.clear()
        self.pool.waitForDone(3000)

    # --- polling ------------------------------------------------------------

    def _due(self, host_id: str, now: float) -> bool:
        if self._in_flight.get(host_id):
            return False
        return now >= self._next_poll.get(host_id, 0.0)

    def tick(self, force: bool = False) -> int:
        """Dispatch one poll per eligible host. Returns how many were started."""
        now = time.time()
        grouped = self.store.active_jobs_by_host()
        if not grouped:
            self.stop()
            return 0

        started = 0
        for host_id, jobs in grouped.items():
            host = self.store.hosts.get(host_id)
            if host is None:
                continue
            if not force and not self._due(host_id, now):
                continue
            if self._in_flight.get(host_id):
                continue
            self._in_flight[host_id] = True
            self.poll_started.emit(host_id)
            # Copies, not the store's own records: the task runs on a pool
            # thread, and the GUI thread is the only writer of the job store.
            task = _PollTask(
                host, [Job.from_dict(job.to_dict()) for job in jobs], self.transport_factory
            )
            task.signals.finished.connect(self._on_poll_finished)
            task.signals.failed.connect(self._on_poll_failed)
            self.pool.start(task)
            started += 1
        return started

    def refresh_now(self) -> bool:
        """Manual refresh, rate limited. Returns False if it was too soon."""
        now = time.time()
        if now - self._last_manual < MANUAL_REFRESH_COOLDOWN:
            return False
        self._last_manual = now
        self._backoff.clear()
        self._next_poll.clear()
        self.tick(force=True)
        self.start()
        return True

    # --- results (GUI thread) -----------------------------------------------

    def _schedule_next(self, host_id: str, delay: float) -> None:
        # +/-10% jitter keeps several hosts from synchronising into a burst.
        jitter = delay * 0.1
        self._next_poll[host_id] = time.time() + max(1.0, delay + random.uniform(-jitter, jitter))

    def _on_poll_finished(
        self,
        host_id: str,
        updates: dict,
        exit_codes: Optional[dict] = None,
        errors: Optional[dict] = None,
    ) -> None:
        self._in_flight[host_id] = False
        self._backoff.pop(host_id, None)
        self._schedule_next(host_id, float(self.store.poll_interval))

        # Before the state loop, and not inside it: the exit code and the reason
        # are worth recording for a job whose *state* has not changed as well --
        # they were dropped for exactly those jobs, which is the resubmitted job
        # that failed the same way twice.
        recorded = False
        for job_id, code in (exit_codes or {}).items():
            job = self.store.jobs.get(job_id)
            if job is not None and job.rc != code:
                job.rc = code
                recorded = True
        for job_id, message in (errors or {}).items():
            job = self.store.jobs.get(job_id)
            if job is not None and message and job.last_error != message:
                job.last_error = message
                recorded = True

        applied: List[tuple] = []
        for job_id, state in (updates or {}).items():
            job = self.store.jobs.get(job_id)
            if job is None or job.state == state:
                continue
            job.touch(state)
            applied.append((job_id, state))
        # Persist before announcing. A listener reacts synchronously -- a
        # finished job starts its auto-download, which moves the job to
        # DOWNLOADING -- so saving afterwards wrote that transient state to
        # disk in place of the real outcome.
        if applied or recorded:
            self.store.save_jobs()
        for job_id, state in applied:
            self.job_updated.emit(job_id, state)
        if not self.store.active_jobs():
            self.stop()
        self.poll_finished.emit(host_id)

    def _on_poll_failed(self, host_id: str, message: str) -> None:
        self._in_flight[host_id] = False
        previous = self._backoff.get(host_id, float(self.store.poll_interval))
        delay = min(MAX_BACKOFF, max(float(self.store.poll_interval), previous * 2))
        self._backoff[host_id] = delay
        self._schedule_next(host_id, delay)
        logging.warning("Job Manager: poll of host %s failed: %s", host_id, message)
        self.host_error.emit(host_id, message)
        self.poll_finished.emit(host_id)

    # --- introspection for the UI / tests -----------------------------------

    def backoff_for(self, host_id: str) -> float:
        return self._backoff.get(host_id, 0.0)

    def is_in_flight(self, host_id: str) -> bool:
        return bool(self._in_flight.get(host_id))


__all__ = ["JobPoller", "MAX_BACKOFF", "MANUAL_REFRESH_COOLDOWN", "ACTIVE_STATES"]
