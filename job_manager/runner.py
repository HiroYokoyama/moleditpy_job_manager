"""Blocking job operations: submit, poll, fetch, cancel, tail.

Everything here takes a :class:`~job_manager.transport.base.Transport` and runs
synchronously, so it must be called from a worker thread. Keeping it free of Qt
is what lets the whole workflow be tested against a fake transport with no
event loop and no network.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Sequence

from . import remote_paths, remote_runner
from .models import (
    SCHEDULER_SHELL,
    SENTINEL_NAME,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_LOST,
    STATE_PENDING,
    STATE_RUNNING,
    STATE_SUBMITTED,
    HostProfile,
    Job,
    SubmitPreset,
    sanitize_name,
)
from .schedulers import STATE_UNKNOWN, get_scheduler
from .transport.base import Transport, TransportError

DEFAULT_LOG_NAME = "job.log"
#: Marks the boundaries of a sentinel sweep so one command covers many jobs.
_SENTINEL_MARK = "@@MOLEDITPY@@"


def make_remote_dir(host: HostProfile, job_name: str, when: Optional[float] = None) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when or time.time()))
    return remote_paths.join(
        host.remote_root or "~/moleditpy_jobs", f"{stamp}_{sanitize_name(job_name)}"
    )


def submit_job(
    transport: Transport,
    host: HostProfile,
    preset: SubmitPreset,
    job: Job,
    local_files: Sequence[str],
    run_after: str = "",
    start_after: float = 0.0,
    run_after_any: bool = False,
) -> Job:
    """Create the remote directory, upload everything, enqueue the script.

    ``run_after`` chains this job behind another process on the same
    machine: the wrapper waits for it before running anything. Only the
    no-queue scheduler uses it -- a real queue does its own serialising.
    ``run_after_any`` asks for a dependency the predecessor satisfies by
    ending rather than by succeeding.
    """
    scheduler = get_scheduler(host.scheduler)
    if not local_files:
        raise ValueError("No input file selected")

    job.remote_dir = job.remote_dir or make_remote_dir(host, job.name)
    job.log_file = job.log_file or DEFAULT_LOG_NAME
    transport.mkdirs(job.remote_dir)

    for path in local_files:
        transport.upload(path, remote_paths.join(job.remote_dir, os.path.basename(path)))

    input_name = os.path.basename(local_files[0])
    script = scheduler.build_script(
        sanitize_name(job.name),
        preset,
        input_name,
        job.log_file,
        run_after=run_after if scheduler.supports_chaining else "",
        start_after=start_after or job.start_after,
        remote_dir=job.remote_dir,
        run_after_any=run_after_any or job.chain_any,
    )
    job.command = script
    script_remote = remote_paths.join(job.remote_dir, scheduler.script_name)
    _upload_text(transport, script, script_remote)

    submit_cmd = scheduler.submit_command(scheduler.script_name, job.log_file)
    result = transport.run(
        f"cd {remote_paths.quote(job.remote_dir)} && {submit_cmd}",
        timeout=max(60, int(host.command_timeout or 60)),
    )
    if not result.ok:
        raise TransportError(
            f"Submission failed (rc={result.rc}): {(result.stderr or result.stdout).strip()[:400]}"
        )

    remote_job_id = scheduler.parse_submit_output(result.stdout, result.stderr)
    if not remote_job_id:
        raise TransportError(
            "Submitted, but the job id could not be read from:\n"
            f"{(result.stdout or result.stderr).strip()[:400]}"
        )

    job.remote_job_id = remote_job_id
    job.submitted_at = time.time()
    job.touch(STATE_SUBMITTED)
    return job


def _upload_text(transport: Transport, text: str, remote_path: str) -> None:
    """Upload an in-memory string, forcing LF endings for the remote shell."""
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", suffix=".sh", delete=False
    )
    try:
        with handle:
            handle.write(text)
        transport.upload(handle.name, remote_path)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            logging.debug("Job Manager: temp script not removed: %s", handle.name)


def short_id(job_id: str) -> str:
    """``12345.head.cluster`` -> ``12345``; qstat truncates the suffix."""
    return (job_id or "").split(".")[0].strip()


def _lookup_state(queue_states: Dict[str, str], job_id: str) -> Optional[str]:
    if job_id in queue_states:
        return queue_states[job_id]
    return queue_states.get(short_id(job_id))


def submit_to_runner(
    transport: Transport,
    host: HostProfile,
    preset: SubmitPreset,
    job: Job,
    local_files: Sequence[str],
    after_job: Optional[Job] = None,
) -> Job:
    """Upload a job and put it in the remote runner's queue.

    The wrapper script is exactly the one the no-queue scheduler builds -- same
    sentinel, same signal traps -- so completion is detected the same way it is
    everywhere else. What changes is who starts it: the queue on the host,
    rather than this submission.

    Chaining is handed to the runner as a header on the queued script, not as a
    ``kill -0`` wait in the wrapper. The runner knows whether the predecessor
    *succeeded*, which a wrapper watching a pid cannot.
    """
    scheduler = get_scheduler(SCHEDULER_SHELL)
    if not local_files:
        raise ValueError("No input file selected")

    job.remote_dir = job.remote_dir or make_remote_dir(host, job.name)
    job.log_file = job.log_file or DEFAULT_LOG_NAME
    transport.mkdirs(job.remote_dir)
    for path in local_files:
        transport.upload(path, remote_paths.join(job.remote_dir, os.path.basename(path)))

    script = scheduler.build_script(
        sanitize_name(job.name),
        preset,
        os.path.basename(local_files[0]),
        job.log_file,
        start_after=job.start_after,
        remote_dir=job.remote_dir,
    )
    job.command = script
    _upload_text(transport, script, remote_paths.join(job.remote_dir, scheduler.script_name))

    directory = remote_runner.runner_dir(host.remote_root)
    transport.run(remote_runner.prepare_command(directory))
    _upload_text(
        transport,
        remote_runner.build_runner_script(directory),
        remote_paths.join(directory, remote_runner.RUNNER_SCRIPT_NAME),
    )
    transport.run(remote_runner.set_slots_command(directory, host.max_concurrent or 1))
    transport.run(remote_runner.set_cores_command(directory, host.runner_cores))

    listing = transport.run(remote_runner.list_command(directory))
    entry = remote_runner.entry_name(
        remote_runner.next_sequence(_entry_names(listing.stdout)), job.id
    )
    job_script = remote_runner.build_job_script(
        job.remote_dir,
        scheduler.script_name,
        job.log_file,
        entry=entry,
        directory=directory,
        job_name=job.name,
        after_job_id=after_job.id if after_job is not None else "",
        require_success=not job.chain_any,
        cores=max(1, int(preset.cpus_per_task or 1)),
    )
    # Into tmp/, then moved: the runner must never see a half-uploaded script.
    _upload_text(transport, job_script, remote_paths.join(directory, "tmp", entry))
    result = transport.run(remote_runner.enqueue_command(directory, entry))
    if not result.ok:
        raise TransportError(
            f"Could not queue the job (rc={result.rc}): "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )

    # Only now: a runner started before the job was queued could empty the
    # queue and exit before it arrived.
    transport.run(remote_runner.ensure_runner_command(directory))

    job.remote_job_id = entry
    job.submitted_at = time.time()
    job.touch(STATE_SUBMITTED)
    return job


def _entry_names(stdout: str) -> List[str]:
    """Every queue entry name in a ``list_command`` result."""
    names = []
    for line in (stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            names.append(parts[1])
    return names


def poll_runner(transport: Transport, host: HostProfile, jobs: Sequence[Job]) -> Dict[str, str]:
    """Where each job is in the remote queue, in one call.

    A job's directory *is* its state: queue, running, or finished. Anything the
    runner no longer lists has ended, and is resolved by the same sentinel
    sweep every other backend uses -- so the exit code is the wrapper's own,
    not the runner's opinion of it.
    """
    tracked = [job for job in jobs if job.remote_job_id]
    if not tracked:
        return {}

    directory = remote_runner.runner_dir(host.remote_root)
    result = transport.run(remote_runner.list_command(directory))
    where = remote_runner.parse_listing(result.stdout)

    updates: Dict[str, str] = {}
    finished: List[Job] = []
    for job in tracked:
        place = where.get(job.id)
        if place == "queue":
            if job.state != STATE_PENDING:
                updates[job.id] = STATE_PENDING
        elif place == "running":
            if job.state != STATE_RUNNING:
                updates[job.id] = STATE_RUNNING
        else:
            finished.append(job)

    if finished:
        outcomes = _read_sentinels(transport, finished)
        blocked = _blocked_entries(transport, directory, finished)
        for job, outcome in zip(finished, outcomes):
            if job.id in blocked:
                # It never ran at all: the runner set it aside because what it
                # was waiting for failed, or was never queued.
                job.last_error = "Queued behind a job that did not succeed; it never started."
                outcome = STATE_FAILED
            if outcome != job.state:
                updates[job.id] = outcome
    return updates


def _blocked_entries(transport: Transport, directory: str, jobs: Sequence[Job]) -> set:
    """Job ids the runner set aside rather than ran."""
    parts = []
    for job in jobs:
        path = remote_paths.quote(remote_paths.join(directory, "status", job.remote_job_id))
        parts.append(f'echo "{_SENTINEL_MARK}"; cat {path} 2>/dev/null || echo MISSING')
    result = transport.run("; ".join(parts))
    chunks = (result.stdout or "").split(_SENTINEL_MARK)[1:]
    blocked = set()
    for index, job in enumerate(jobs):
        raw = chunks[index].strip() if index < len(chunks) else ""
        if raw.splitlines() and raw.splitlines()[0].strip() == remote_runner.STATUS_BLOCKED:
            blocked.add(job.id)
    return blocked


def cancel_in_runner(transport: Transport, host: HostProfile, job: Job) -> None:
    """Cancel a job whether it is waiting in the queue or already running.

    Taking a waiting job out of the queue frees its slot at once -- the thing
    chained lanes cannot do, since there the successor is bound to a specific
    predecessor.
    """
    directory = remote_runner.runner_dir(host.remote_root)
    transport.run(remote_runner.cancel_command(directory, job.remote_job_id))


def poll_host(transport: Transport, host: HostProfile, jobs: Sequence[Job]) -> Dict[str, str]:
    """Resolve the state of every active job on one host.

    Exactly two round trips at most: one queue listing, plus one sentinel sweep
    for the jobs that have left the queue since the last poll.

    Returns a mapping of ``Job.id`` -> new state. Jobs whose state is unchanged
    are omitted.
    """
    scheduler = get_scheduler(host.scheduler)
    tracked = [job for job in jobs if job.remote_job_id]
    if not tracked:
        return {}

    status_cmd = scheduler.status_command(
        host.username or "$USER", [job.remote_job_id for job in tracked]
    )
    result = transport.run(status_cmd)
    # An empty queue makes squeue/qstat exit non-zero on some sites, so a
    # failure with no output is treated as "nothing queued", not an error.
    if not result.ok and (result.stderr or "").strip() and not (result.stdout or "").strip():
        lowered = result.stderr.lower()
        if "unknown job" not in lowered and "no unfinished" not in lowered:
            raise TransportError(
                f"Status query failed (rc={result.rc}): {result.stderr.strip()[:300]}"
            )

    queue_states = scheduler.parse_status(result.stdout)

    updates: Dict[str, str] = {}
    finished: List[Job] = []
    for job in tracked:
        state = _lookup_state(queue_states, job.remote_job_id)
        if state is None:
            finished.append(job)
        elif state != STATE_UNKNOWN and state != job.state:
            updates[job.id] = state

    if finished:
        for job, outcome in zip(finished, _read_sentinels(transport, finished)):
            if outcome != job.state:
                updates[job.id] = outcome
    return updates


def _read_sentinels(transport: Transport, jobs: Sequence[Job]) -> List[str]:
    """One command reads every finished job's exit-code file."""
    parts: List[str] = []
    for job in jobs:
        sentinel = remote_paths.quote(remote_paths.join(job.remote_dir, SENTINEL_NAME))
        parts.append(f'echo "{_SENTINEL_MARK}"; cat {sentinel} 2>/dev/null || echo MISSING')
    result = transport.run("; ".join(parts))

    chunks = (result.stdout or "").split(_SENTINEL_MARK)[1:]
    outcomes: List[str] = []
    for index, job in enumerate(jobs):
        raw = chunks[index].strip() if index < len(chunks) else "MISSING"
        outcomes.append(_classify_sentinel(raw, job))
    return outcomes


def _classify_sentinel(raw: str, job: Job) -> str:
    token = (raw or "").strip().splitlines()[0].strip() if raw.strip() else "MISSING"
    if token == "MISSING":
        # Gone from the queue without the wrapper finishing: killed, evicted, or
        # the directory vanished. Distinguish a user cancel from the rest.
        return STATE_CANCELLED if job.state == STATE_CANCELLED else STATE_LOST
    try:
        code = int(token)
    except ValueError:
        return STATE_LOST
    job.rc = code
    return STATE_DONE if code == 0 else STATE_FAILED


def list_remote_files(transport: Transport, remote_dir: str) -> List[str]:
    """File names directly inside ``remote_dir`` (no recursion).

    Only plain names are returned. The listing comes from the remote machine,
    and every name is later joined onto a local directory to download into --
    so anything with a path separator in it (``../../.bashrc``) would write
    outside the download folder.
    """
    result = transport.run(f"ls -p -1 {remote_paths.quote(remote_dir)} 2>/dev/null || true")
    names = []
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if not name or name.endswith("/"):
            continue
        if name != safe_download_name(name):
            logging.warning("Job Manager: skipping suspicious remote file name %r", name)
            continue
        names.append(name)
    return names


def safe_download_name(name: str) -> str:
    """The bare file name, or "" if ``name`` is not one."""
    cleaned = (name or "").replace("\\", "/").strip()
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("/"):
        return ""
    if "/" in cleaned or os.path.splitdrive(cleaned)[0]:
        return ""
    return cleaned


def select_files(names: Iterable[str], globs: Sequence[str]) -> List[str]:
    patterns = [g.strip() for g in (globs or []) if g and g.strip()]
    if not patterns:
        return list(names)
    selected = []
    for name in names:
        if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            selected.append(name)
    return selected


def fetch_results(
    transport: Transport, job: Job, local_dir: str, globs: Optional[Sequence[str]] = None
) -> List[str]:
    """Download everything in the job directory matching the fetch globs."""
    patterns = [p for p in (globs if globs is not None else (job.fetch_globs or [])) if p.strip()]
    # The log is what the user reads first; always bring it back -- but adding
    # it to an empty pattern list would turn "no filter, fetch everything" into
    # "fetch the log and nothing else".
    if patterns and job.log_file and job.log_file not in patterns:
        patterns.append(job.log_file)

    names = select_files(list_remote_files(transport, job.remote_dir), patterns)
    os.makedirs(local_dir, exist_ok=True)
    downloaded: List[str] = []
    for name in names:
        # Belt and braces: the listing is already filtered, but this is the
        # line that turns a remote string into a local path to write.
        if not safe_download_name(name):
            continue
        target = os.path.join(local_dir, name)
        try:
            transport.download(remote_paths.join(job.remote_dir, name), target)
        except TransportError:
            logging.warning("Job Manager: could not download %s", name)
            continue
        downloaded.append(target)
    return downloaded


def cancel_job(transport: Transport, host: HostProfile, job: Job) -> None:
    scheduler = get_scheduler(host.scheduler)
    if not job.remote_job_id:
        return
    result = transport.run(scheduler.cancel_command(job.remote_job_id))
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()
        # Already gone is a success from the user's point of view.
        if "invalid job id" in detail.lower() or "unknown job" in detail.lower():
            return
        raise TransportError(f"Cancel failed: {detail[:300]}")


def tail_log(transport: Transport, job: Job, lines: int = 200) -> str:
    if not job.remote_dir or not job.log_file:
        return ""
    path = remote_paths.quote(remote_paths.join(job.remote_dir, job.log_file))
    result = transport.run(f"tail -n {int(lines)} {path} 2>&1 || true")
    return result.stdout or result.stderr or ""


#: Re-exported so callers do not need the models module for the common states.
__all__ = [
    "DEFAULT_LOG_NAME",
    "STATE_PENDING",
    "STATE_RUNNING",
    "cancel_job",
    "fetch_results",
    "list_remote_files",
    "make_remote_dir",
    "poll_host",
    "select_files",
    "short_id",
    "submit_job",
    "tail_log",
]
