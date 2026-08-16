"""Blocking job operations: submit, poll, fetch, cancel, tail.

Everything here takes a :class:`~job_manager.transport.base.Transport` and runs
synchronously, so it must be called from a worker thread. Keeping it free of Qt
is what lets the whole workflow be tested against a fake transport with no
event loop and no network.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Sequence

from . import dialect, remote_paths, remote_runner
from .models import (
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
from .schedulers import STATE_UNKNOWN, get_scheduler, requested_cores, requested_memory_mb
from .transport.base import Transport, TransportError

DEFAULT_LOG_NAME = "job.log"
#: Downloads are written under this and renamed on success, so a half-finished
#: transfer never wears the name of a finished result.
PARTIAL_SUFFIX = ".moleditpy-part"
#: Marks the boundaries of a sentinel sweep so one command covers many jobs.
_SENTINEL_MARK = "@@MOLEDITPY@@"


def make_remote_dir(
    host: HostProfile, job_name: str, when: Optional[float] = None, job_id: str = ""
) -> str:
    """Where one job's files live on the host: ``<root>/<stamp>_<name>_<id>``.

    The job id is in the name because the stamp is only accurate to the second,
    and two jobs of the same name submitted within one second -- a batch, a
    loop, two clicks -- landed in *one* directory. They then overwrote each
    other's wrapper and inputs and, worse, shared a single ``.moleditpy_rc``:
    whichever finished first decided what both jobs were reported to have done.

    The timestamp stays in front so the directory listing is still in the order
    the jobs were submitted, which is what makes it readable by hand.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when or time.time()))
    name = f"{stamp}_{sanitize_name(job_name)}"
    if job_id:
        name = f"{name}_{sanitize_name(job_id, fallback='')}"
    return remote_paths.join(host.remote_root or "~/moleditpy_jobs", name)


def input_name_for(job: Job, local_files: Sequence[str]) -> str:
    """What ``{input}`` means for this job.

    A name the user gave for a file already on the host wins over an uploaded
    one: it is the explicit answer, and the whole point of naming it. Empty is
    allowed -- a command-only job has no input file at all, and the templates
    that do not mention one run perfectly well without.
    """
    if job.remote_input:
        return safe_relative_name(job.remote_input)
    return os.path.basename(local_files[0]) if local_files else ""


def name_job_files(job: Job, scheduler) -> None:
    """Decide what the wrapper writes, and under what names.

    In a directory this plugin made for the job, the shared defaults are
    fine: nothing else is in there. In one the *user* prepared they are not.
    That directory holds their files, and very likely other jobs submitted
    into it -- and two jobs sharing one ``.moleditpy_rc`` means whichever
    finishes first decides what both are reported to have done. So everything
    written there carries the job id.
    """
    if not job.remote_dir_provided:
        job.log_file = job.log_file or DEFAULT_LOG_NAME
        return
    tag = sanitize_name(job.id, fallback="job")
    stem, extension = os.path.splitext(scheduler.script_name)
    job.script_name = job.script_name or f"{stem}_{tag}{extension}"
    job.log_file = job.log_file or f"moleditpy_{tag}.log"
    job.sentinel_name = job.sentinel_name or f"{SENTINEL_NAME}_{tag}"


def sentinel_for(job: Job) -> str:
    """The completion file this job writes; the shared name for older jobs."""
    return job.sentinel_name or SENTINEL_NAME


def script_name_for(job: Job, scheduler) -> str:
    return job.script_name or scheduler.script_name


def require_remote_path(
    transport: Transport, host: HostProfile, path: str, directory: bool = False
) -> None:
    """Fail before submitting if a path the user typed is not on the host.

    Only for paths they typed. ``mkdir -p`` would otherwise make the typo,
    and the job would run in a new empty directory with none of the files it
    was prepared with -- reported as a clean failure of the calculation
    rather than as the mistake it is.
    """
    result = transport.run(dialect.for_host(host).exists(path, directory=directory))
    if dialect.PRESENT not in (result.stdout or ""):
        what = "directory" if directory else "file"
        raise TransportError(f"No such {what} on {host.name}: {path}")


def prepare_remote_dir(transport: Transport, host: HostProfile, job: Job) -> None:
    """Make the job's directory, or check the one the user named is there."""
    if job.remote_dir_provided and job.remote_dir:
        require_remote_path(transport, host, job.remote_dir, directory=True)
        if job.remote_input:
            safe_input = safe_relative_name(job.remote_input)
            if not safe_input:
                raise ValueError(
                    "Remote input must be a relative file name inside the remote directory"
                )
            job.remote_input = safe_input
            require_remote_path(
                transport, host, remote_paths.join(job.remote_dir, safe_input)
            )
        return
    job.remote_dir = job.remote_dir or make_remote_dir(host, job.name, job_id=job.id)
    transport.mkdirs(job.remote_dir)


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

    Input files are optional: a job may instead run a command over work the
    user has already staged on the host (``job.remote_dir_provided``).
    """
    scheduler = get_scheduler(host.scheduler)
    if not (preset.command_template or "").strip():
        raise ValueError("No command to run")

    name_job_files(job, scheduler)
    prepare_remote_dir(transport, host, job)

    for path in local_files:
        transport.upload(path, remote_paths.join(job.remote_dir, os.path.basename(path)))

    input_name = input_name_for(job, local_files)
    script = scheduler.build_script(
        sanitize_name(job.name),
        preset,
        input_name,
        job.log_file,
        run_after=run_after if scheduler.supports_chaining else "",
        start_after=start_after or job.start_after,
        remote_dir=job.remote_dir,
        run_after_any=run_after_any or job.chain_any,
        sentinel=sentinel_for(job),
        preamble=host.environment_commands(),
    )
    job.command = script
    script_name = script_name_for(job, scheduler)
    script_remote = remote_paths.join(job.remote_dir, script_name)
    _upload_text(transport, script, script_remote)

    submit_cmd = scheduler.submit_command(script_name, job.log_file)
    result = transport.run(
        dialect.for_host(host).run_in(job.remote_dir, submit_cmd),
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
    scheduler = get_scheduler(host.scheduler)
    flavour = remote_runner.flavour_for(host)
    if not (preset.command_template or "").strip():
        raise ValueError("No command to run")

    name_job_files(job, scheduler)
    prepare_remote_dir(transport, host, job)
    for path in local_files:
        transport.upload(path, remote_paths.join(job.remote_dir, os.path.basename(path)))

    script = scheduler.build_script(
        sanitize_name(job.name),
        preset,
        input_name_for(job, local_files),
        job.log_file,
        start_after=job.start_after,
        remote_dir=job.remote_dir,
        sentinel=sentinel_for(job),
        preamble=host.environment_commands(),
    )
    job.command = script
    # Not `script_name`: that name belongs to the runner's own script below.
    job_script_name = script_name_for(job, scheduler)
    _upload_text(transport, script, remote_paths.join(job.remote_dir, job_script_name))

    directory = remote_runner.runner_dir(host.remote_root)
    setup = transport.run(
        flavour.setup_command(
            directory,
            remote_runner.slots_for(host),
            host.runner_cores,
            host.runner_memory_mb,
        )
    )
    runner_script = flavour.build_runner_script(directory)
    digest = _digest(runner_script)
    # Named after its own contents, so a new version never writes over the file
    # a running runner is part way through -- bash reads a script by byte
    # offset as it goes, and replacing it underneath resumes in the middle of
    # different text. Old versions stay on the host, next to the queue entries
    # they ran.
    script_name = flavour.runner_script_name(digest)
    if (setup.stdout or "").strip().splitlines()[-1:] != [digest]:
        # Only when it would differ. The script is the same bytes on every
        # submission to the same host, and re-uploading it was an scp per job.
        _upload_text(transport, runner_script, remote_paths.join(directory, script_name))
        transport.run(flavour.store_digest_command(directory, digest))

    # Claimed on the host, not worked out from a listing: the number is the
    # dispatch order, and one derived from the queue restarts the moment a user
    # clears done/ -- putting the next job ahead of everything still waiting.
    claimed = transport.run(flavour.claim_sequence_command(directory))
    sequence = remote_runner.parse_sequence(claimed.stdout)
    if not sequence:
        raise TransportError(
            "Could not take a queue number on the host: "
            f"{(claimed.stderr or claimed.stdout).strip()[:300]}"
        )
    entry = remote_runner.entry_name(sequence, job.id, flavour.ENTRY_SUFFIX)
    job_script = flavour.build_job_script(
        job.remote_dir,
        job_script_name,
        job.log_file,
        entry=entry,
        directory=directory,
        job_name=job.name,
        after_job_id=after_job.id if after_job is not None else "",
        require_success=not job.chain_any,
        cores=requested_cores(preset),
        memory_mb=requested_memory_mb(preset),
    )
    # Into tmp/, then moved: the runner must never see a half-uploaded script.
    _upload_text(transport, job_script, remote_paths.join(directory, "tmp", entry))
    result = transport.run(flavour.enqueue_command(directory, entry))
    if not result.ok:
        raise TransportError(
            f"Could not queue the job (rc={result.rc}): "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )

    # Only now: a runner started before the job was queued could empty the
    # queue and exit before it arrived.
    # The versioned name, not the default: the script is content-addressed, so
    # starting "the runner" by a fixed name starts a file that is not there.
    started = transport.run(flavour.ensure_runner_command(directory, script_name))
    if "missing" in (started.stdout or ""):
        # The queue would sit there for ever otherwise, with the job showing
        # PENDING and nothing on the host to move it.
        raise TransportError(
            f"The job was queued, but the helper script {script_name} is not on the host, "
            "so nothing will start it."
        )

    job.remote_job_id = entry
    job.submitted_at = time.time()
    job.touch(STATE_SUBMITTED)
    return job


def _digest(text: str) -> str:
    """Identifies one runner script. Short: it is compared, never trusted."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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
    result = transport.run(remote_runner.flavour_for(host).list_command(directory))
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
    speak = dialect.for_host(transport.host)
    paths = [remote_paths.join(directory, "status", job.remote_job_id) for job in jobs]
    result = transport.run(speak.read_files(paths, _SENTINEL_MARK))
    chunks = (result.stdout or "").split(_SENTINEL_MARK)[1:]
    blocked = set()
    for index, job in enumerate(jobs):
        raw = chunks[index].strip() if index < len(chunks) else ""
        if raw.splitlines() and raw.splitlines()[0].strip() == remote_runner.STATUS_BLOCKED:
            blocked.add(job.id)
    return blocked


def queue_paused(transport: Transport, host: HostProfile) -> bool:
    """Whether the host's runner is currently holding its queue."""
    directory = remote_runner.runner_dir(host.remote_root)
    result = transport.run(remote_runner.flavour_for(host).is_paused_command(directory))
    # A runner that has never been set up prints nothing at all, and "no queue
    # yet" is not "the queue is held".
    return (result.stdout or "").strip().splitlines()[-1:] == [remote_runner.PAUSED_NAME]


def set_queue_paused(transport: Transport, host: HostProfile, paused: bool) -> bool:
    """Hold the host's queue, or let it move again. Returns the new state.

    The runner re-reads the flag between jobs, so this reaches a runner that is
    already up without restarting it -- and a runner that has since exited
    leaves the flag behind for the next one to find.
    """
    flavour = remote_runner.flavour_for(host)
    directory = remote_runner.runner_dir(host.remote_root)
    # The flag lives in the runner directory, which need not exist yet: pausing
    # a host before its first submission has to be allowed, or the only way to
    # hold a queue would be to start it first.
    transport.run(flavour.prepare_command(directory))
    result = transport.run(flavour.pause_command(directory, paused))
    if not result.ok:
        raise TransportError(
            f"Could not change the queue (rc={result.rc}): "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )
    return bool(paused)


def probe_resources(transport: Transport, host: HostProfile) -> tuple:
    """Ask the host what it has: ``(cores, memory_mb)``, 0 where unknown.

    The same question the helper answers for itself when a budget is left at
    "detect" -- asked out loud, so the user can see the numbers, keep them, or
    set a smaller share of a machine they do not have to themselves.
    """
    result = transport.run(remote_runner.flavour_for(host).probe_command(), timeout=30)
    return remote_runner.parse_probe(result.stdout)


def apply_queue_limits(transport: Transport, host: HostProfile) -> None:
    """Push this host's job and core limits to a runner that is already up.

    Submitting sends them too, but a limit changed between submissions would
    otherwise not take effect until the next one -- which is exactly when the
    user no longer needs it.
    """
    flavour = remote_runner.flavour_for(host)
    directory = remote_runner.runner_dir(host.remote_root)
    transport.run(
        flavour.setup_command(
            directory,
            remote_runner.slots_for(host),
            host.runner_cores,
            host.runner_memory_mb,
        )
    )


def cancel_in_runner(transport: Transport, host: HostProfile, job: Job) -> None:
    """Cancel a job whether it is waiting in the queue or already running.

    Taking a waiting job out of the queue frees its slot at once -- the thing
    chained lanes cannot do, since there the successor is bound to a specific
    predecessor.
    """
    directory = remote_runner.runner_dir(host.remote_root)
    transport.run(remote_runner.flavour_for(host).cancel_command(directory, job.remote_job_id))


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
    speak = dialect.for_host(transport.host)
    paths = [remote_paths.join(job.remote_dir, sentinel_for(job)) for job in jobs]
    result = transport.run(speak.read_files(paths, _SENTINEL_MARK))

    chunks = (result.stdout or "").split(_SENTINEL_MARK)[1:]
    outcomes: List[str] = []
    for index, job in enumerate(jobs):
        raw = chunks[index].strip() if index < len(chunks) else dialect.MISSING
        outcomes.append(_classify_sentinel(raw, job))
    return outcomes


def _classify_sentinel(raw: str, job: Job) -> str:
    token = (raw or "").strip().splitlines()[0].strip() if raw.strip() else dialect.MISSING
    if token == dialect.MISSING:
        # Gone from the queue without the wrapper finishing: killed, evicted, or
        # the directory vanished. Distinguish a user cancel from the rest.
        return STATE_CANCELLED if job.state == STATE_CANCELLED else STATE_LOST
    try:
        code = int(token)
    except ValueError:
        return STATE_LOST
    job.rc = code
    return STATE_DONE if code == 0 else STATE_FAILED


#: How deep a fetch pattern may reach. A pattern is allowed to name a
#: sub-directory, but not to turn a download into a walk of a scratch tree.
MAX_FETCH_DEPTH = 4


def list_remote_files(transport: Transport, remote_dir: str, depth: int = 1) -> List[str]:
    """File names inside ``remote_dir``, ``depth`` levels down.

    Depth 1 is the plain listing and stays the default: recursion is a more
    expensive command, and it is only worth it when a fetch pattern names a
    sub-directory.

    Whatever the depth, every name is validated before it is returned. The
    listing comes from the remote machine and is later joined onto a local
    directory to download into, so a name like ``../../.bashrc`` would write
    outside the download folder.
    """
    speaker = dialect.for_host(transport.host)
    if depth > 1:
        result = transport.run(speaker.list_tree(remote_dir, depth))
    else:
        result = transport.run(speaker.list_dir(remote_dir))
    names = []
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if not name or name.endswith("/"):
            continue
        safe = safe_relative_name(name) if depth > 1 else safe_download_name(name)
        if name.replace("\\", "/") != safe:
            logging.warning("Job Manager: skipping suspicious remote file name %r", name)
            continue
        names.append(safe)
    return names


def safe_download_name(name: str) -> str:
    """The bare file name, or "" if ``name`` is not one."""
    cleaned = (name or "").replace("\\", "/").strip()
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("/"):
        return ""
    if "/" in cleaned or os.path.splitdrive(cleaned)[0]:
        return ""
    return cleaned


def safe_relative_name(name: str) -> str:
    """The same, but allowing sub-directories: ``scratch/mol.out``.

    Every name here comes from the remote machine and is then joined onto a
    local directory to write into, so the rules are the ones that keep it
    inside: nothing absolute, no drive letter, and no ``..`` in any segment.
    The result is always spelled with forward slashes.
    """
    cleaned = (name or "").replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or os.path.splitdrive(cleaned)[0]:
        return ""
    segments = [part for part in cleaned.split("/") if part not in ("", ".")]
    if not segments or any(part == ".." for part in segments):
        return ""
    return "/".join(segments)


def matches_pattern(name: str, pattern: str) -> bool:
    """fnmatch, but a ``*`` stops at a directory boundary.

    ``fnmatch`` alone treats the whole string as one word, so ``*.out`` would
    match ``scratch/mol.out`` the moment the listing went one level deep --
    quietly changing what every existing pattern means. Matching segment by
    segment keeps ``*.out`` meaning "in the job directory", and makes
    ``scratch/*.out`` and ``*/*.out`` say what they look like they say.

    ``**`` stands for any number of directories, as it does everywhere else.
    """
    name_parts = [p for p in (name or "").split("/") if p]
    pattern_parts = [p for p in (pattern or "").split("/") if p]

    def match_from(name_index: int, pattern_index: int) -> bool:
        while pattern_index < len(pattern_parts):
            part = pattern_parts[pattern_index]
            if part == "**":
                # Try consuming nothing, then one segment, then two...
                for skip in range(name_index, len(name_parts) + 1):
                    if match_from(skip, pattern_index + 1):
                        return True
                return False
            if name_index >= len(name_parts):
                return False
            if not fnmatch.fnmatch(name_parts[name_index], part):
                return False
            name_index += 1
            pattern_index += 1
        return name_index == len(name_parts)

    return match_from(0, 0)


def pattern_depth(globs: Sequence[str]) -> int:
    """How deep the listing has to go for these patterns. 1 is no recursion.

    Depth costs a recursive listing on the far end, so it is taken from what
    was actually asked for rather than applied always. ``**`` is capped: a
    pattern that means "anywhere" must not turn a fetch into a walk of a
    scratch directory with a hundred thousand files in it.
    """
    depth = 1
    for pattern in globs or []:
        parts = [p for p in (pattern or "").strip().split("/") if p]
        if any(part == "**" for part in parts):
            return MAX_FETCH_DEPTH
        depth = max(depth, len(parts))
    return min(depth, MAX_FETCH_DEPTH)


def select_files(names: Iterable[str], globs: Sequence[str]) -> List[str]:
    patterns = [g.strip() for g in (globs or []) if g and g.strip()]
    if not patterns:
        return list(names)
    selected = []
    for name in names:
        if any(matches_pattern(name, pattern) for pattern in patterns):
            selected.append(name)
    return selected


def fetch_results(
    transport: Transport, job: Job, local_dir: str, globs: Optional[Sequence[str]] = None
) -> List[str]:
    """Download everything in the job directory matching the fetch globs."""
    patterns = [p for p in (globs if globs is not None else (job.fetch_globs or [])) if p.strip()]

    # Only as deep as the patterns actually reach: recursion is a more
    # expensive command on the far end, and most fetches want one directory.
    depth = pattern_depth(patterns)
    names = select_files(list_remote_files(transport, job.remote_dir, depth), patterns)
    # The wrapper's own log is not a result: it holds whatever the command
    # wrote to stdout and stderr, while the calculation's real output is the
    # file the command was told to write. It used to be forced into every
    # download, and `*.log` in the default patterns fetched it besides -- so a
    # directory of results carried a job.log next to the .out nobody wanted to
    # tell apart. It stays on the host, where Tail Log reads it live.
    #
    # Never automatically: it is this plugin's file rather than the
    # calculation's output, and a results directory should hold only what the
    # job produced. No wildcard reaches it -- not `*.log`, which is there for
    # Gaussian's output, and not an empty pattern list either.
    #
    # Named exactly it is fetched, because that is somebody asking for it: the
    # download chooser lists it and passes back what was ticked.
    if job.log_file and job.log_file not in patterns:
        names = [name for name in names if name != job.log_file]
    os.makedirs(local_dir, exist_ok=True)
    # Results are downloaded next to the input by default, so the job's own
    # inputs are sitting in the target directory -- and a fetch glob of *.xyz
    # against an input named mol.xyz would otherwise write the remote copy back
    # over the user's file. It is the same bytes today, but a truncated
    # download would destroy the original.
    protected = {os.path.abspath(path) for path in (job.input_files or []) if path}
    downloaded: List[str] = []
    for name in names:
        # Belt and braces: the listing is already filtered, but this is the
        # line that turns a remote string into a local path to write.
        safe = safe_relative_name(name)
        if not safe:
            continue
        target = os.path.join(local_dir, *safe.split("/"))
        # And the check that actually holds: whatever the name looked like, the
        # file has to land inside the directory we were asked to write into.
        root = os.path.abspath(local_dir)
        if os.path.commonpath([root, os.path.abspath(target)]) != root:
            logging.warning("Job Manager: refusing to write outside %s: %r", local_dir, name)
            continue
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.abspath(target) in protected:
            logging.debug("Job Manager: not overwriting the input file %s", name)
            continue
        # Into a part file, then renamed. Results land in the directory the
        # user is working in, so a transfer cut off half way would otherwise
        # leave a truncated .out sitting there under its real name, looking
        # exactly like a complete one -- and over the top of the previous
        # attempt's good copy.
        staging = target + PARTIAL_SUFFIX
        try:
            transport.download(remote_paths.join(job.remote_dir, name), staging)
            os.replace(staging, target)
        except (TransportError, OSError):
            logging.warning("Job Manager: could not download %s", name)
            _discard(staging)
            continue
        downloaded.append(target)
    return downloaded


def _discard(path: str) -> None:
    """Remove a part file, if it got as far as existing."""
    try:
        os.unlink(path)
    except OSError:
        logging.debug("Job Manager: part file not removed: %s", path)


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
    safe_log = safe_relative_name(job.log_file)
    if not job.remote_dir or not safe_log:
        return ""
    path = remote_paths.join(job.remote_dir, safe_log)
    result = transport.run(dialect.for_host(transport.host).tail(path, lines))
    return result.stdout or result.stderr or ""


def tail_remote_file(transport: Transport, job: Job, filename: str, lines: int = 200) -> str:
    safe_name = safe_relative_name(filename)
    if not job.remote_dir or not safe_name:
        return ""
    path = remote_paths.join(job.remote_dir, safe_name)
    result = transport.run(dialect.for_host(transport.host).tail(path, lines))
    return result.stdout or result.stderr or ""


#: Re-exported so callers do not need the models module for the common states.
__all__ = [
    "DEFAULT_LOG_NAME",
    "PARTIAL_SUFFIX",
    "STATE_PENDING",
    "STATE_RUNNING",
    "apply_queue_limits",
    "cancel_job",
    "fetch_results",
    "input_name_for",
    "list_remote_files",
    "make_remote_dir",
    "name_job_files",
    "poll_host",
    "prepare_remote_dir",
    "require_remote_path",
    "script_name_for",
    "sentinel_for",
    "queue_paused",
    "select_files",
    "set_queue_paused",
    "short_id",
    "submit_job",
    "tail_log",
    "tail_remote_file",
]

