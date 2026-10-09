"""Submission orchestration; the JobService remains the public QObject facade."""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from .models import STATE_FAILED, STATE_UPLOADING, HostProfile, Job, SubmitPreset, sanitize_name
from .runner import submit_job, submit_to_runner
from .tasks import run_async


class SubmissionWorkflow:
    def __init__(self, service):
        self.service = service

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
        force_run: bool = False,
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

        ``force_run`` starts the job ahead of the helper queue on a host that
        has one. Elsewhere it is only recorded: a host with no queue starts a
        job at once anyway unless it is chained, and choosing not to chain is
        the caller's part.
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
            force_run=bool(force_run),
        )
        job.touch(STATE_UPLOADING)
        self.service.store.add_job(job)
        self.service.jobs_changed.emit()
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
            run_after = "" if host.uses_remote_runner else self.service._chain_pid(after_job)
            transport = self.service.transport_for(host)
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
                        force=job.force_run,
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
            self.service.pool,
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
        # Monotonic: a clock set back by NTP or a DST bug would stretch the wait.
        deadline = time.monotonic() + timeout
        while not after_job.remote_job_id and time.monotonic() < deadline:
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
        if self.service.store.get_pref("download_beside_input", True):
            for path in local_files or []:
                directory = os.path.dirname(os.path.abspath(path))
                if directory and os.path.isdir(directory) and os.access(directory, os.W_OK):
                    return directory
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.service.store.download_root(), f"{stamp}_{sanitize_name(name)}")

    def _on_submitted(self, job: Job) -> None:
        stored = self.service.store.jobs.get(job.id)
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
        self.service.store.save_jobs()
        self.service.message.emit(f"Submitted {job.name} as {job.remote_job_id}")
        # prime, not start: the first status query comes seconds later, not a full interval.
        self.service.poller.prime(job.host_id)
        self.service.job_updated.emit(job.id)
        self.service.jobs_changed.emit()

    def _on_submit_failed(self, job_id: str, message: str) -> None:
        job = self.service.store.jobs.get(job_id)
        if job is not None:
            job.last_error = message
            job.touch(STATE_FAILED)
            self.service.store.save_jobs()
            self.service.job_updated.emit(job_id)
        self.service.error.emit(f"Submission failed: {message}")
        self.service.jobs_changed.emit()
