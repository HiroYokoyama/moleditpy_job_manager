"""Session-scoped coordinator: store + poller + transports + the round trip.

The service outlives the job window on purpose. Closing the monitor must not
stop tracking, so the plugin module owns one service for the whole application
session and the dialog is only a view onto it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Sequence


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
    MAX_FETCH_DEPTH,
    cancel_in_runner,
    cancel_job,
    fetch_results,
    list_remote_files,
    release_in_runner,
    require_remote_path,
    submit_job,
    submit_to_runner,
    tail_log,
    tail_remote_file,
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
        self._downloads_in_flight: set = set()
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

        For the wizard, when a job is about to be pointed at a directory the
        user typed from memory.
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
        relay_source_dir: str = "",
        relay_filenames: Optional[List[str]] = None,
        upload_files: Optional[List[str]] = None,
    ) -> Job:
        """Create the job record and start the upload/submit on a worker.

        ``after_job`` chains this one behind another job on the same host;
        ``chain_any`` accepts the predecessor merely ending, not succeeding.
        ``remote_dir``/``remote_input`` run the job in a directory already on
        the host rather than a new one. ``relay_source_dir``/``relay_filenames``
        copy files from a previous job's directory into this one's first.

        ``upload_files``, when given, is what actually goes to the host instead
        of ``local_files`` -- a relay uploads a substituted temp copy, but the
        job still belongs to the file the user chose (so results land beside
        that input, not the scratch copy).
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
        # Basename is the same either way, so {input} means the same thing for both.
        to_upload = list(upload_files) if upload_files else list(local_files)

        def work() -> Job:
            # Resolved here, not at dispatch: reading the pid too early (before
            # submission) chained the second of two quick submissions behind
            # nothing, so both ran at once.
            #
            # And before opening the transport, not after: this can wait up to
            # two minutes and needs no connection, so opening first held an
            # idle ssh/ControlMaster the whole time.
            run_after = "" if host.uses_remote_runner else self._chain_pid(after_job)
            transport = self.transport_for(host)
            try:
                if host.uses_remote_runner:
                    # Takes the dependency by job id and resolves it itself.
                    return submit_to_runner(
                        transport,
                        host,
                        preset,
                        job,
                        to_upload,
                        after_job=after_job,
                        relay_source_dir=relay_source_dir,
                        relay_filenames=relay_filenames or (),
                    )
                return submit_job(
                    transport,
                    host,
                    preset,
                    job,
                    to_upload,
                    run_after=run_after,
                    start_after=job.start_after,
                    run_after_any=job.chain_any,
                    relay_source_dir=relay_source_dir,
                    relay_filenames=relay_filenames or (),
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

        Called from a worker thread, so blocking is fine. If the predecessor's
        own submission failed and it never gets a pid, this job simply runs.
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

        Beside the input by default. Falls back to the download root when
        there's nothing to sit beside, or that directory isn't writable.
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
            # The poller reads the sentinel by this name; wrong name reads as LOST.
            stored.script_name = job.script_name
            stored.sentinel_name = job.sentinel_name
            stored.command = job.command
            stored.submitted_at = job.submitted_at
            stored.touch(job.state)
        self.store.save_jobs()
        self.message.emit(f"Submitted {job.name} as {job.remote_job_id}")
        # prime, not start: the first status query comes seconds later, not a full interval.
        self.poller.prime(job.host_id)
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
        # Must run before download() starts, which moves the job to DOWNLOADING:
        # with auto-download on, chain_blocker read a job that was no longer
        # FAILED here, and the one chance to warn about a dead chain was lost.
        finished = state in TERMINAL_STATES
        if finished:
            # Only from a poll: a failed submission is caught while the user
            # is still in the wizard, not here.
            self.job_finished.emit(job.id, state)
        if finished and state != STATE_DONE:
            self._warn_stranded(job)
        if state in (STATE_DONE, STATE_FAILED) and job.auto_download and not job.downloaded:
            self.download(job)

    def _warn_stranded(self, job: Job) -> None:
        """Say so when a failure has left the jobs behind it unable to start.

        Under ``afterok`` the queue keeps reporting those as PENDING forever;
        nothing is cancelled automatically, but the user has to be told.
        """
        # The whole chain, recursively: everything after the first blocked job
        # is stranded by the same failure, not just that one job.
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

    def list_remote_results(self, job: Job, on_ok, on_error) -> None:
        """What is in the job directory, for the download chooser."""
        host = self.store.hosts.get(job.host_id)
        if host is None:
            on_error(f"Host profile for {job.name} no longer exists")
            return

        def work() -> List[str]:
            transport = self.transport_for(host)
            try:
                # As deep as a pattern could ever reach, so the tree shows
                # everything, including directories no pattern covers.
                names = list_remote_files(transport, job.remote_dir, MAX_FETCH_DEPTH)
            finally:
                transport.close()
            # The wrapper's log is listed too, so it can be picked deliberately
            # without editing fetch patterns (it's never matched by one).
            return names

        run_async(self.pool, work, on_success=on_ok, on_error=on_error)

    def download(self, job: Job, into: str = "", names: Optional[Sequence[str]] = None) -> bool:
        """Fetch the job's outputs; emits ``results_ready`` when they land.

        ``into`` is the folder the user picked; without it, the automatic
        choice applies (beside the input, or the shared download folder).

        Returns whether a download actually started -- False if one is already
        running, so a caller need not wait forever on ``results_ready`` (bug
        once left the Open Result window stuck on "Downloading...").
        """
        host = self.store.hosts.get(job.host_id)
        if host is None:
            self.error.emit(f"Host profile for {job.name} no longer exists")
            return False
        if job.id in self._downloads_in_flight:
            self.message.emit(f"Download already in progress for {job.name}")
            return False
        self._downloads_in_flight.add(job.id)
        previous_state = job.state
        local_dir = into or job.local_dir or self._local_dir_for(job.name, job.input_files)
        job.local_dir = local_dir
        job.touch(STATE_DOWNLOADING)
        self.job_updated.emit(job.id)

        def work() -> List[str]:
            transport = self.transport_for(host)
            try:
                # An explicit set of names is passed as patterns: each matches only itself.
                return fetch_results(transport, job, local_dir, globs=names)
            finally:
                transport.close()

        def done(paths: List[str]) -> None:
            self._downloads_in_flight.discard(job.id)
            job.downloaded = bool(paths)
            job.downloaded_files = list(paths or [])
            job.touch(previous_state)
            self.store.save_jobs()
            self.job_updated.emit(job.id)
            self.message.emit(f"Downloaded {len(job.downloaded_files)} file(s) for {job.name}")
            self.results_ready.emit(job.id, job.downloaded_files)

        def failed(message: str) -> None:
            self._downloads_in_flight.discard(job.id)
            job.last_error = message
            job.touch(previous_state)
            self.store.save_jobs()
            self.job_updated.emit(job.id)
            self.error.emit(f"Download failed: {message}")

        run_async(self.pool, work, on_success=done, on_error=failed)
        return True

    def fetch_file_to_cache(self, job: Job, filename: str, on_ok, on_error) -> None:
        """Fetch one remote file into a local temporary cache directory."""
        import tempfile

        host = self.store.hosts.get(job.host_id)
        if host is None:
            on_error(f"Host profile for {job.name} no longer exists")
            return

        cache_dir = os.path.join(tempfile.gettempdir(), "moleditpy_job_manager_cache", job.id)
        os.makedirs(cache_dir, exist_ok=True)

        def work() -> str:
            from .runner import safe_relative_name

            safe_name = safe_relative_name(filename)
            if not safe_name:
                raise ValueError(f"Unsafe remote filename: {filename}")
            transport = self.transport_for(host)
            try:
                paths = fetch_results(transport, job, cache_dir, globs=[safe_name])
                if paths and os.path.isfile(paths[0]):
                    return paths[0]
                local_path = os.path.join(cache_dir, *safe_name.split("/"))
                if os.path.isfile(local_path):
                    return local_path
                raise FileNotFoundError(
                    f"Remote file '{filename}' was not found or could not be downloaded"
                )
            finally:
                transport.close()

        run_async(self.pool, work, on_success=on_ok, on_error=on_error)

    def cancel(self, job: Job, release_dependents: bool = True) -> None:
        """Cancel one job, and by default let the jobs behind it carry on.

        Cancelling the middle job used to take the rest of the chain with it,
        since a cancelled job leaves no exit code for the helper queue to
        check. Dependents are released on the host, so it holds even with
        MoleditPy closed, unless the caller says otherwise.
        """
        host = self.store.hosts.get(job.host_id)
        if host is None:
            self.error.emit(f"Host profile for {job.name} no longer exists")
            return
        dependents = self.store.dependents_of(job.id) if release_dependents else []
        if dependents:
            # Recorded before the cancel: stops the monitor calling them
            # blocked, and must survive a restart.
            for dependent in dependents:
                dependent.chain_any = True
                dependent.touch()
            self.store.save_jobs()

        def work() -> None:
            transport = self.transport_for(host)
            try:
                if host.uses_remote_runner:
                    cancel_in_runner(transport, host, job)
                    for dependent in dependents:
                        release_in_runner(transport, host, dependent)
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

        run_async(self.pool, work, on_success=self.log_ready.emit, on_error=self.error.emit)

    def tail_file(
        self,
        job: Job,
        filename: str,
        lines: int = 200,
        on_done: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Asynchronously read the tail of any remote file in the job's directory."""
        host = self.store.hosts.get(job.host_id)
        if host is None:
            err = "Host profile no longer exists"
            (on_error or self.error.emit)(err)
            return

        def work() -> str:
            transport = self.transport_for(host)
            try:
                return tail_remote_file(transport, job, filename, lines)
            finally:
                transport.close()

        success_handler = on_done or self.log_ready.emit
        error_handler = on_error or self.error.emit
        run_async(self.pool, work, on_success=success_handler, on_error=error_handler)

    # --- housekeeping -------------------------------------------------------

    def remove_job(self, job_id: str) -> None:
        self.store.remove_job(job_id)
        self.jobs_changed.emit()

    def shutdown(self) -> None:
        self.poller.shutdown()
        self.pool.clear()
        self.pool.waitForDone(3000)
        logging.debug("Job Manager: service shut down")
