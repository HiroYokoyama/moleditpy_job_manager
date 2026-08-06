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
    HostProfile,
    Job,
    SubmitPreset,
    sanitize_name,
)
from .poller import JobPoller
from .runner import cancel_job, fetch_results, submit_job, tail_log
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

    # --- submission ---------------------------------------------------------

    def submit(
        self,
        host: HostProfile,
        preset: SubmitPreset,
        name: str,
        local_files: List[str],
        auto_download: Optional[bool] = None,
    ) -> Job:
        """Create the job record and start the upload/submit on a worker."""
        job = Job(
            name=name or (os.path.basename(local_files[0]) if local_files else "job"),
            host_id=host.id,
            host_name=host.name,
            scheduler=host.scheduler,
            input_files=list(local_files),
            fetch_globs=list(preset.fetch_globs),
            auto_download=preset.auto_download if auto_download is None else auto_download,
            local_dir=self._local_dir_for(name or "job"),
            preset=preset.to_dict(),
        )
        job.touch(STATE_UPLOADING)
        self.store.add_job(job)
        self.jobs_changed.emit()

        def work() -> Job:
            transport = self.transport_for(host)
            try:
                return submit_job(transport, host, preset, job, local_files)
            finally:
                transport.close()

        run_async(
            self.pool,
            work,
            on_success=self._on_submitted,
            on_error=lambda msg, job_id=job.id: self._on_submit_failed(job_id, msg),
        )
        return job

    def _local_dir_for(self, name: str) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.store.download_root(), f"{stamp}_{sanitize_name(name)}")

    def _on_submitted(self, job: Job) -> None:
        stored = self.store.jobs.get(job.id)
        if stored is not None and stored is not job:
            # The worker mutated its own copy; carry the results across.
            stored.remote_dir = job.remote_dir
            stored.remote_job_id = job.remote_job_id
            stored.log_file = job.log_file
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
        if state in (STATE_DONE, STATE_FAILED) and job.auto_download and not job.downloaded:
            self.download(job)

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
        local_dir = job.local_dir or self._local_dir_for(job.name)
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
