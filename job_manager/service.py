"""Session-scoped coordinator: store + poller + transports + the round trip.

The service outlives the job window on purpose. Closing the monitor must not
stop tracking, so the plugin module owns one service for the whole application
session and the dialog is only a view onto it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal

from .models import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_UPLOADING,
    TERMINAL_STATES,
    HostProfile,
    Job,
    SubmitPreset,
    sanitize_name,
)
from .poller import JobPoller
from .runner import (
    cancel_in_runner,
    cancel_job,
    fetch_results,
    list_remote_files,
    require_remote_path,
    submit_job,
    submit_to_runner,
    tail_log,
)
from .store import JobStore
from .tasks import run_async
from .transport import create_transport


class JobService(QObject):
    """Owns all job state and every network operation."""

    jobs_changed = pyqtSignal()
    job_updated = pyqtSignal(str)  # job_id
    message = pyqtSignal(str)  # human-readable status line
    error = pyqtSignal(str)
    #: A finished job's files are on disk and ready to open.
    results_ready = pyqtSignal(str, list)  # job_id, local paths
    #: A tracked job reached a terminal state while the user was elsewhere.
    job_finished = pyqtSignal(str, str)  # job_id, state
    log_ready = pyqtSignal(str)  # tail of a remote log

    def __init__(self, store: Optional[JobStore] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.store = store or JobStore()
        self.store.prune()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        #: host_id -> password, session lifetime only. Never persisted.
        self._passwords: Dict[str, str] = {}
        # Late-bound on purpose: passing the bound method would freeze the
        # factory at construction time and ignore any later override.
        self.poller = JobPoller(self.store, lambda host: self.transport_for(host), parent=self)
        self.poller.job_updated.connect(self._on_job_state_changed)
        self.poller.host_error.connect(self._on_host_error)
        self.poller.start()

    # --- transports ---------------------------------------------------------

    def set_password(self, host_id: str, password: str) -> None:
        """Remember a paramiko password for this session only."""
        if password:
            self._passwords[host_id] = password
        else:
            self._passwords.pop(host_id, None)

    def has_password(self, host_id: str) -> bool:
        return host_id in self._passwords

    def transport_for(self, host: HostProfile):
        return create_transport(host, password=self._passwords.get(host.id))

    # --- inspection ---------------------------------------------------------

    def list_remote_dir(self, host: HostProfile, path: str, on_done, on_error=None) -> None:
        """Names in a remote directory, or an error if it is not one.

        For the wizard, where a job is about to be pointed at a directory the
        user typed from memory. Being told now that it holds four files, or
        that it is not there at all, is the difference between fixing a typo
        and reading a failed job's log tomorrow.
        """

        def work() -> List[str]:
            transport = self.transport_for(host)
            try:
                require_remote_path(transport, host, path, directory=True)
                return list_remote_files(transport, path)
            finally:
                transport.close()

        run_async(self.pool, work, on_success=on_done, on_error=on_error or self.error.emit)

    # --- submission ---------------------------------------------------------

    def submit(
        self,
        host: HostProfile,
        preset: SubmitPreset,
        name: str,
        local_files: List[str],
        auto_download: Optional[bool] = None,
        after_job: Optional[Job] = None,
        start_after: float = 0.0,
        chain_any: bool = False,
        remote_dir: str = "",
        remote_input: str = "",
    ) -> Job:
        """Create the job record and start the upload/submit on a worker.

        ``after_job`` chains this one behind another job on the same host: a
        queue is told to hold it, the no-queue mode has the wrapper wait for
        that job's process. ``chain_any`` makes the predecessor merely having
        ended enough, instead of it having succeeded.

        ``remote_dir`` runs the job in a directory already on the host, rather
        than in a new one -- work the user staged there themselves, which
        ``local_files`` need not (and usually does not) duplicate.
        ``remote_input`` names a file in it for ``{input}``.
        """
        job = Job(
            name=name or self._default_name(local_files, remote_input, remote_dir),
            host_id=host.id,
            host_name=host.name,
            scheduler=host.scheduler,
            input_files=list(local_files),
            fetch_globs=list(preset.fetch_globs),
            auto_download=preset.auto_download if auto_download is None else auto_download,
            local_dir=self._local_dir_for(name or "job", local_files),
            preset=preset.to_dict(),
            after_job_id=after_job.id if after_job is not None else "",
            chain_any=bool(chain_any),
            start_after=float(start_after or 0.0),
            remote_dir=(remote_dir or "").strip(),
            remote_dir_provided=bool((remote_dir or "").strip()),
            remote_input=(remote_input or "").strip(),
        )
        job.touch(STATE_UPLOADING)
        self.store.add_job(job)
        self.jobs_changed.emit()

        def work() -> Job:
            # Resolved here, not at dispatch: submitting twice in quick
            # succession queues both workers before the first has a pid, and
            # reading it too early chained the second job behind nothing at
            # all -- so both ran at once, which is the one thing chaining is
            # for.
            #
            # And *before* the transport is opened, not after: this waits for
            # another job's submission, for up to two minutes, and needs no
            # connection to do it. Opening first held an idle ssh -- and, with
            # the OpenSSH backend, a ControlMaster process -- for the whole wait.
            run_after = "" if host.uses_remote_runner else self._chain_pid(after_job)
            transport = self.transport_for(host)
            try:
                if host.uses_remote_runner:
                    # The runner takes the dependency by job id and resolves it
                    # itself, so there is no pid to wait for here.
                    return submit_to_runner(
                        transport, host, preset, job, local_files, after_job=after_job
                    )
                return submit_job(
                    transport,
                    host,
                    preset,
                    job,
                    local_files,
                    run_after=run_after,
                    start_after=job.start_after,
                    run_after_any=job.chain_any,
                )
            finally:
                transport.close()

        run_async(
            self.pool,
            work,
            on_success=self._on_submitted,
            on_error=lambda msg, job_id=job.id: self._on_submit_failed(job_id, msg),
        )
        return job

    @staticmethod
    def _default_name(local_files: List[str], remote_input: str, remote_dir: str) -> str:
        """A name for a job the user did not name, from whatever it is about."""
        if local_files:
            return os.path.basename(local_files[0])
        if remote_input:
            return os.path.basename(remote_input)
        if remote_dir:
            return os.path.basename(remote_dir.rstrip("/\\")) or "job"
        return "job"

    def _chain_pid(self, after_job: Optional[Job], timeout: float = 120.0) -> str:
        """The predecessor's remote pid, waiting for its submission if needed.

        Called from a worker thread, so blocking here is fine. A predecessor
        that never gets a pid -- its own submission failed -- is not something
        to wait for, and this job simply runs.
        """
        if after_job is None:
            return ""
        deadline = time.time() + timeout
        while not after_job.remote_job_id and time.time() < deadline:
            if after_job.is_terminal:
                logging.warning(
                    "Job Manager: %s never started, so the job chained behind it will not wait",
                    after_job.name,
                )
                return ""
            time.sleep(0.2)
        return after_job.remote_job_id

    def _local_dir_for(self, name: str, local_files: Optional[List[str]] = None) -> str:
        """Where this job's results will land.

        Beside the input by default: that is the directory the user is already
        working in, and hunting through a central store for the outputs of the
        file you just submitted is a small indignity repeated every time.

        Falls back to the download root whenever there is nothing to sit beside
        -- a job resubmitted after its input moved, or an input directory that
        is not writable.
        """
        if self.store.get_pref("download_beside_input", True):
            for path in local_files or []:
                directory = os.path.dirname(os.path.abspath(path))
                if directory and os.path.isdir(directory) and os.access(directory, os.W_OK):
                    return directory
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.store.download_root(), f"{stamp}_{sanitize_name(name)}")

    def _on_submitted(self, job: Job) -> None:
        stored = self.store.jobs.get(job.id)
        if stored is not None and stored is not job:
            # The worker mutated its own copy; carry the results across.
            stored.remote_dir = job.remote_dir
            stored.remote_job_id = job.remote_job_id
            stored.log_file = job.log_file
            # Not cosmetic: the poller reads the sentinel by this name, and a
            # stored job left on the shared default would look LOST for ever.
            stored.script_name = job.script_name
            stored.sentinel_name = job.sentinel_name
            stored.command = job.command
            stored.submitted_at = job.submitted_at
            stored.touch(job.state)
        self.store.save_jobs()
        self.message.emit(f"Submitted {job.name} as {job.remote_job_id}")
        self.poller.start()
        self.job_updated.emit(job.id)
        self.jobs_changed.emit()

    def _on_submit_failed(self, job_id: str, message: str) -> None:
        job = self.store.jobs.get(job_id)
        if job is not None:
            job.last_error = message
            job.touch(STATE_FAILED)
            self.store.save_jobs()
            self.job_updated.emit(job_id)
        self.error.emit(f"Submission failed: {message}")
        self.jobs_changed.emit()

    # --- state changes ------------------------------------------------------

    def _on_job_state_changed(self, job_id: str, state: str) -> None:
        self.job_updated.emit(job_id)
        job = self.store.jobs.get(job_id)
        if job is None:
            return
        # Everything that depends on the outcome happens *before* the download
        # is started, because starting one moves this job to DOWNLOADING there
        # and then. With auto-download on -- the default, and what every real
        # job has -- job.is_terminal was already False by the time it was
        # asked, so nothing was announced; and _warn_stranded went on to ask
        # chain_blocker, which reads the predecessor's current state and saw a
        # job that was no longer FAILED. The one warning that says a chain is
        # dead is emitted once, in this window, so it was lost for good.
        finished = state in TERMINAL_STATES
        if finished:
            # Only from a poll: this is the transition the user is not watching.
            # A submission that fails does so while they are still in the wizard.
            self.job_finished.emit(job.id, state)
        if finished and state != STATE_DONE:
            self._warn_stranded(job)
        if state in (STATE_DONE, STATE_FAILED) and job.auto_download and not job.downloaded:
            self.download(job)

    def _warn_stranded(self, job: Job) -> None:
        """Say so when a failure has left the jobs behind it unable to start.

        Under ``afterok`` the queue keeps reporting those as PENDING for ever.
        Nothing is cancelled automatically -- the user may well want to fix the
        input and resubmit -- but they have to be told it will not happen.
        """
        # The whole chain, not just the job immediately behind: everything
        # after that one is stranded by the same failure, and being told about
        # the first while the rest sit silently at QUEUED is the half-truth
        # this warning exists to avoid.
        for dependent in self.store.dependents_of(job.id, recursive=True):
            if self.store.chain_blocker(dependent) is not None:
                self.error.emit(
                    f"{dependent.name} was queued behind {job.name}, which "
                    f"{job.state.lower()}: it will never start. Cancel it, or "
                    f"resubmit without chaining."
                )
                self.job_updated.emit(dependent.id)

    def _on_host_error(self, host_id: str, message: str) -> None:
        host = self.store.hosts.get(host_id)
        label = host.name if host else host_id
        self.message.emit(f"{label}: {message}")

    # --- results ------------------------------------------------------------

    def download(self, job: Job) -> None:
        """Fetch the job's outputs; emits ``results_ready`` when they land."""
        host = self.store.hosts.get(job.host_id)
        if host is None:
            self.error.emit(f"Host profile for {job.name} no longer exists")
            return
        previous_state = job.state
        local_dir = job.local_dir or self._local_dir_for(job.name, job.input_files)
        job.local_dir = local_dir
        job.touch(STATE_DOWNLOADING)
        self.job_updated.emit(job.id)

        def work() -> List[str]:
            transport = self.transport_for(host)
            try:
                return fetch_results(transport, job, local_dir)
            finally:
                transport.close()

        def done(paths: List[str]) -> None:
            job.downloaded = True
            job.downloaded_files = list(paths or [])
            job.touch(previous_state)
            self.store.save_jobs()
            self.job_updated.emit(job.id)
            self.message.emit(f"Downloaded {len(job.downloaded_files)} file(s) for {job.name}")
            self.results_ready.emit(job.id, job.downloaded_files)

        def failed(message: str) -> None:
            job.last_error = message
            job.touch(previous_state)
            self.store.save_jobs()
            self.job_updated.emit(job.id)
            self.error.emit(f"Download failed: {message}")

        run_async(self.pool, work, on_success=done, on_error=failed)

    def cancel(self, job: Job) -> None:
        host = self.store.hosts.get(job.host_id)
        if host is None:
            self.error.emit(f"Host profile for {job.name} no longer exists")
            return

        def work() -> None:
            transport = self.transport_for(host)
            try:
                if host.uses_remote_runner:
                    cancel_in_runner(transport, host, job)
                else:
                    cancel_job(transport, host, job)
            finally:
                transport.close()

        def done(_result) -> None:
            job.touch(STATE_CANCELLED)
            self.store.save_jobs()
            self.job_updated.emit(job.id)
            self.message.emit(f"Cancelled {job.name}")

        run_async(self.pool, work, on_success=done, on_error=self.error.emit)

    def tail(self, job: Job, lines: int = 200) -> None:
        """Asynchronously read the tail of the remote log; result via message."""
        host = self.store.hosts.get(job.host_id)
        if host is None:
            self.error.emit("Host profile no longer exists")
            return

        def work() -> str:
            transport = self.transport_for(host)
            try:
                return tail_log(transport, job, lines)
            finally:
                transport.close()

        return run_async(self.pool, work, on_success=self.log_ready.emit, on_error=self.error.emit)

    # --- housekeeping -------------------------------------------------------

    def remove_job(self, job_id: str) -> None:
        self.store.remove_job(job_id)
        self.jobs_changed.emit()

    def shutdown(self) -> None:
        self.poller.shutdown()
        self.pool.clear()
        self.pool.waitForDone(3000)
        logging.debug("Job Manager: service shut down")
