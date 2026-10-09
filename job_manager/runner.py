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

from . import PLUGIN_VERSION, dialect, remote_paths, remote_runner
from .models import (
    BACKEND_LOCAL,
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
from .schedulers import (
    STATE_UNKNOWN,
    format_command,
    get_scheduler,
    requested_cores,
    requested_memory_mb,
    submit_arguments,
)
from .transport.base import Transport, TransportError

DEFAULT_LOG_NAME = "job.log"
#: Downloads are written under this and renamed on success, so a half-finished
#: transfer never wears the name of a finished result.
PARTIAL_SUFFIX = ".moleditpy-part"
#: Marks the boundaries of a sentinel sweep so one command covers many jobs.
_SENTINEL_MARK = "@@MOLEDITPY@@"


def effective_root(host: HostProfile) -> str:
    """The remote root to actually build paths from.

    For every real remote host this is just ``host.remote_root``. For a local
    host it is resolved to an absolute path first: a leading ``~`` there would
    otherwise be expanded twice, by two resolvers not guaranteed to agree --
    bash's own ``$HOME`` for every command ``LocalTransport.run()`` sends, and
    Python's ``os.path.expanduser()`` for every file ``upload()``/``download()``
    moves. Where they differ -- ``$HOME`` set explicitly, a profile that
    redirects it, anything short of the ordinary case -- ``mkdir`` and upload
    land in one directory while ``cd`` and ``chmod`` look in another that
    happens to exist, and submission fails on every job with "No such file or
    directory" for a script that really is on disk, just not where the shell
    was told to look for it. Resolving it once, here, with the same call the
    transport itself uses, means both sides are always given the identical
    absolute path.
    """
    root = host.remote_root or "~/moleditpy_jobs"
    if host.backend == BACKEND_LOCAL:
        root = host.local_root() or os.path.abspath(os.path.expanduser(root))
        # Forward slashes throughout: the path is about to be embedded in
        # shell commands built by posixpath, and a mix of separators is one
        # more way for the shell's idea of the path and the file that is
        # actually there to quietly stop being the same string.
        root = root.replace("\\", "/")
    return root


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
    return remote_paths.join(effective_root(host), name)


#: Characters that would make a file name act as *syntax* in the command it is
#: substituted into. ``{input}`` goes onto the command line as it stands -- it
#: has to, since a template is free to quote it itself -- so a file called
#: ``mol$(id).inp`` would run `id` on the host.
#:
#: A space is deliberately not on this list. A template that writes
#: ``"{input}"`` handles a name with a space in it perfectly well, and refusing
#: those would turn an ordinary file name into a submission error. Nor are the
#: glob characters: they can only ever name another file in the same directory,
#: which is not the same kind of thing at all.
UNSAFE_IN_COMMAND = "\"'`$;&|<>()\\\n\r"


def command_unsafe_character(name: str) -> str:
    """The first character of ``name`` a remote shell would act on, or ""."""
    for character in name or "":
        if character in UNSAFE_IN_COMMAND:
            return character
    return ""


def check_input_name(name: str) -> None:
    """Raise if this file name cannot safely be substituted into a command."""
    bad = command_unsafe_character(name)
    if not bad:
        return
    raise ValueError(
        f"The input file is named {name!r}, and a remote shell reads {bad!r} in "
        "it as syntax rather than as part of the name. The name is substituted "
        "into the command line for {input}, so the rest of it would run as a "
        "command of its own. Rename the file and submit again."
    )


def input_name_for(job: Job, local_files: Sequence[str]) -> str:
    """What ``{input}`` means for this job.

    A name the user gave for a file already on the host wins over an uploaded
    one: it is the explicit answer, and the whole point of naming it. Empty is
    allowed -- a command-only job has no input file at all, and the templates
    that do not mention one run perfectly well without.
    """
    name = (
        safe_relative_name(job.remote_input)
        if job.remote_input
        else (os.path.basename(local_files[0]) if local_files else "")
    )
    check_input_name(name)
    return name


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
        job.log_file = safe_relative_name(job.log_file) or DEFAULT_LOG_NAME
        job.script_name = safe_relative_name(job.script_name)
        job.sentinel_name = safe_relative_name(job.sentinel_name)
        return
    tag = sanitize_name(job.id, fallback="job")
    stem, extension = os.path.splitext(scheduler.script_name)
    job.script_name = safe_relative_name(job.script_name) or f"{stem}_{tag}{extension}"
    job.log_file = safe_relative_name(job.log_file) or f"moleditpy_{tag}.log"
    job.sentinel_name = safe_relative_name(job.sentinel_name) or f"{SENTINEL_NAME}_{tag}"


def sentinel_for(job: Job) -> str:
    """The completion file this job writes; the shared name for older jobs."""
    return safe_relative_name(job.sentinel_name) or SENTINEL_NAME


def script_name_for(job: Job, scheduler) -> str:
    return safe_relative_name(job.script_name) or scheduler.script_name


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
            require_remote_path(transport, host, remote_paths.join(job.remote_dir, safe_input))
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
    relay_source_dir: str = "",
    relay_filenames: Sequence[str] = (),
) -> Job:
    """Create the remote directory, upload everything, enqueue the script.

    ``run_after`` chains this job behind another process on the same
    machine: the wrapper waits for it before running anything. Only the
    no-queue scheduler uses it -- a real queue does its own serialising.
    ``run_after_any`` asks for a dependency the predecessor satisfies by
    ending rather than by succeeding.

    Input files are optional: a job may instead run a command over work the
    user has already staged on the host (``job.remote_dir_provided``).

    ``relay_source_dir``/``relay_filenames`` copy files from a previous job's
    own directory into this one's -- written *into the generated script*
    itself, not run ahead of submission, which is what lets this relay from a
    job that has not finished yet: whatever gates this job's start (a queue
    dependency, or the wrapper's own wait for a pid) already guarantees the
    predecessor is done by the time those lines run.
    """
    scheduler = get_scheduler(host.scheduler)
    if not (preset.command_template or "").strip():
        raise ValueError("No command to run")

    name_job_files(job, scheduler)
    # Before the directory is made and anything is uploaded: a name that cannot
    # go into the command line is not a job, and leaving its files on the host
    # would be litter nobody goes back for.
    input_name = input_name_for(job, local_files)
    # Before anything reaches the host, for the same reason: an unbalanced
    # quote in the options is a typo to report, not a directory to litter.
    # Only where there is a queue to hand them to -- the built-in modes ignore
    # them, so a typo there must not stop a submission that never uses them.
    options = (host.submit_options, preset.submit_options) if scheduler.queue_directives else ()
    try:
        submit_arguments(*options)
    except ValueError as exc:
        raise ValueError(f"Submit options could not be read: {exc}") from None
    prepare_remote_dir(transport, host, job)
    # After the directory is decided, so {jobdir} has something to say.
    extra_args = submit_arguments(
        *options,
        substitute=lambda word: format_command(
            word, input_name, preset, sanitize_name(job.name), job.remote_dir
        ),
    )

    for path in local_files:
        transport.upload(path, remote_paths.join(job.remote_dir, os.path.basename(path)))

    relay_lines = (
        dialect.for_host(host).relay_lines(relay_source_dir, relay_filenames)
        if relay_source_dir and relay_filenames
        else []
    )
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
        relay_lines=relay_lines,
    )
    job.command = script
    script_name = script_name_for(job, scheduler)
    script_remote = remote_paths.join(job.remote_dir, script_name)
    _upload_text(transport, script, script_remote)

    submit_cmd = scheduler.submit_command(script_name, job.log_file, extra_args)
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
    relay_source_dir: str = "",
    relay_filenames: Sequence[str] = (),
    force: bool = False,
) -> Job:
    """Upload a job and put it in the remote runner's queue.

    The wrapper script is exactly the one the no-queue scheduler builds -- same
    sentinel, same signal traps -- so completion is detected the same way it is
    everywhere else. What changes is who starts it: the queue on the host,
    rather than this submission.

    Chaining is handed to the runner as a header on the queued script, not as a
    ``kill -0`` wait in the wrapper. The runner knows whether the predecessor
    *succeeded*, which a wrapper watching a pid cannot.

    ``relay_source_dir``/``relay_filenames``: see :func:`submit_job`.

    ``force`` starts the job the moment it is queued, ahead of everything
    waiting and past every limit -- see :func:`remote_runner.force_command`.
    """
    scheduler = get_scheduler(host.scheduler)
    flavour = remote_runner.flavour_for(host)
    if not (preset.command_template or "").strip():
        raise ValueError("No command to run")

    name_job_files(job, scheduler)
    # Before anything is uploaded; see submit_job.
    input_name = input_name_for(job, local_files)
    prepare_remote_dir(transport, host, job)
    for path in local_files:
        transport.upload(path, remote_paths.join(job.remote_dir, os.path.basename(path)))

    relay_lines = (
        dialect.for_host(host).relay_lines(relay_source_dir, relay_filenames)
        if relay_source_dir and relay_filenames
        else []
    )
    script = scheduler.build_script(
        sanitize_name(job.name),
        preset,
        input_name,
        job.log_file,
        start_after=job.start_after,
        remote_dir=job.remote_dir,
        sentinel=sentinel_for(job),
        preamble=host.environment_commands(),
        relay_lines=relay_lines,
    )
    job.command = script
    # Not `script_name`: that name belongs to the runner's own script below.
    job_script_name = script_name_for(job, scheduler)
    _upload_text(transport, script, remote_paths.join(job.remote_dir, job_script_name))

    directory = remote_runner.runner_dir(effective_root(host))
    script_name = _prepare_runner(transport, host)

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

    if force:
        # Before the helper is started, so it cannot take the job in its turn
        # first. A helper already up may still win the claim; then the job
        # simply started the ordinary way, which is not worth an error.
        transport.run(flavour.force_command(directory, entry))
        job.force_run = True

    # Only now: a runner started before the job was queued could empty the
    # queue and exit before it arrived.
    _start_runner(transport, host, script_name, "The job was queued")

    job.remote_job_id = entry
    job.submitted_at = time.time()
    job.touch(STATE_SUBMITTED)
    return job


def _prepare_runner(transport: Transport, host: HostProfile) -> str:
    """Make the helper's directories, push the limits, and make sure the current
    helper script is on the host. Returns that script's name."""
    flavour = remote_runner.flavour_for(host)
    directory = remote_runner.runner_dir(effective_root(host))
    setup = transport.run(
        flavour.setup_command(
            directory,
            remote_runner.slots_for(host),
            host.runner_cores,
            host.runner_memory_mb,
        )
    )
    # Named after the plugin's own version, not a content hash: the script is
    # fixed for the life of a release, so this is an equally reliable way to
    # tell whether the host already has the current one, and it is a name a
    # user reading the directory over plain ssh can actually place.
    script_name = flavour.runner_script_name(PLUGIN_VERSION)
    if (setup.stdout or "").strip().splitlines()[-1:] != [PLUGIN_VERSION]:
        # Only when it would differ. The script is the same bytes on every
        # submission to the same host, and re-uploading it was an scp per job.
        # The version this replaces is left where it is, deliberately: a
        # script that ran a job is worth keeping, since the queue is readable
        # over plain ssh precisely so a user can see what ran.
        runner_script = flavour.build_runner_script(directory)
        _upload_text(transport, runner_script, remote_paths.join(directory, script_name))
        transport.run(flavour.store_version_command(directory, PLUGIN_VERSION))
    return script_name


def _start_runner(transport: Transport, host: HostProfile, script_name: str, what: str) -> None:
    """Make sure a helper is up, and say so plainly when one cannot be."""
    flavour = remote_runner.flavour_for(host)
    directory = remote_runner.runner_dir(effective_root(host))
    # The versioned name, not the default: the script is named per version, so
    # starting "the runner" by a fixed name starts a file that is not there.
    started = transport.run(flavour.ensure_runner_command(directory, script_name))
    if "missing" in (started.stdout or ""):
        # The queue would sit there for ever otherwise, with the job showing
        # PENDING and nothing on the host to move it.
        raise TransportError(
            f"{what}, but the helper script {script_name} is not on the host, "
            "so nothing will start it."
        )


def force_in_runner(transport: Transport, host: HostProfile, job: Job) -> bool:
    """Start a job that is waiting in the helper queue, now.

    Returns False when it was no longer waiting -- already started, or
    cancelled -- which is not an error: there is nothing left to force.

    A helper is made sure of afterwards, because something has to reap the
    job when it ends; with none up, its entry would sit in ``running/`` and
    the job would read RUNNING for ever.
    """
    if not job.remote_job_id:
        return False
    flavour = remote_runner.flavour_for(host)
    directory = remote_runner.runner_dir(effective_root(host))
    script_name = _prepare_runner(transport, host)
    result = transport.run(flavour.force_command(directory, job.remote_job_id))
    lines = (result.stdout or "").strip().splitlines()
    if lines[-1:] != [remote_runner.FORCED]:
        if lines[-1:] == [remote_runner.NOT_QUEUED]:
            return False
        raise TransportError(
            f"Could not start the job (rc={result.rc}): "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )
    _start_runner(transport, host, script_name, "The job was started")
    return True


def runner_queue(transport: Transport, host: HostProfile) -> dict:
    """The helper queue on ``host``: what runs, what waits in what order, the limits."""
    directory = remote_runner.runner_dir(effective_root(host))
    result = transport.run(remote_runner.flavour_for(host).queue_detail_command(directory))
    return remote_runner.parse_queue_detail(result.stdout)


def recheck_job(transport: Transport, host: HostProfile, job: Job) -> dict:
    """Look again at a job reported LOST, and say what the host has for it now.

    LOST means the queue no longer listed the job and its exit-code file was
    not there. Both can be momentary -- a file a networked filesystem has not
    shown this client yet, a queue that answered half a listing -- so a job
    that actually finished could be left reading LOST for good. This asks the
    queue again, reads the exit-code file again, and on a helper queue also
    the exit code the helper recorded itself, which is a second witness kept
    in a different directory.

    Works on a copy: what to do with the answer is the caller's decision.
    Returns ``state`` and ``rc`` (what the job should now read), the raw
    ``sentinel`` and ``runner_status`` contents, and the job directory's
    ``files``.
    """
    probe = Job.from_dict(job.to_dict())
    probe.state = STATE_RUNNING
    probe.rc = None
    on_runner = bool(remote_runner.parse_entry(probe.remote_job_id)[1])
    if on_runner:
        updates = poll_runner(transport, host, [probe])
    else:
        updates = poll_host(transport, host, [probe])
    state = updates.get(probe.id, STATE_RUNNING)

    speak = dialect.for_host(transport.host)
    paths = [remote_paths.join(job.remote_dir, sentinel_for(job))]
    if on_runner:
        directory = remote_runner.runner_dir(effective_root(host))
        paths.append(remote_paths.join(directory, "status", job.remote_job_id))
    result = transport.run(speak.read_files(paths, _SENTINEL_MARK))
    chunks = [chunk.strip() for chunk in (result.stdout or "").split(_SENTINEL_MARK)[1:]]
    sentinel = chunks[0].splitlines()[0].strip() if chunks and chunks[0] else dialect.MISSING
    runner_status = ""
    if on_runner:
        raw = chunks[1] if len(chunks) > 1 and chunks[1] else dialect.MISSING
        runner_status = raw.splitlines()[0].strip()
        if state == STATE_LOST and runner_status.lstrip("-").isdigit():
            # The wrapper's own file is the better witness and is read first;
            # the helper's record stands in only when that one is not there.
            probe.rc = int(runner_status)
            state = STATE_DONE if probe.rc == 0 else STATE_FAILED

    files: List[str] = []
    if job.remote_dir:
        files = list_remote_files(transport, job.remote_dir)
    return {
        "state": state,
        "rc": probe.rc,
        "sentinel": sentinel,
        "runner_status": runner_status,
        "files": files,
        "last_error": probe.last_error if probe.last_error != job.last_error else "",
    }


def list_host_path(transport: Transport, host: HostProfile, path: str, depth: int = 1) -> List[str]:
    """What is under a directory on the host, anywhere the user can read.

    One level marks sub-directories with a trailing ``/``; deeper lists files
    only, named relative to ``path``. The names are shown, never written to a
    local path, so they are returned as the host spelled them.
    """
    require_remote_path(transport, host, path, directory=True)
    speaker = dialect.for_host(transport.host)
    depth = max(1, min(int(depth or 1), MAX_FETCH_DEPTH))
    if depth > 1:
        result = transport.run(speaker.list_tree(path, depth))
    else:
        result = transport.run(speaker.list_dir(path))
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def stat_host_path(transport: Transport, host: HostProfile, path: str, digest: bool = True) -> dict:
    """Whether a path is there, what it is, and for a file its size and sha256."""
    result = transport.run(dialect.for_host(transport.host).stat(path, digest))
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    kind = lines[0] if lines else dialect.MISSING
    info: dict = {"path": path, "exists": kind in (dialect.FILE, dialect.DIRECTORY)}
    if kind == dialect.DIRECTORY:
        info["type"] = "directory"
    elif kind == dialect.FILE:
        info["type"] = "file"
        info["size"] = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None
        if digest:
            found = lines[2].lower() if len(lines) > 2 else ""
            # Absent rather than wrong when no digest tool answered.
            info["sha256"] = (
                found if len(found) == 64 and all(c in "0123456789abcdef" for c in found) else ""
            )
    else:
        info["type"] = ""
    return info


def download_host_paths(
    transport: Transport,
    host: HostProfile,
    paths: Sequence[str],
    local_dir: str,
    overwrite: bool = False,
) -> tuple:
    """Fetch named files from anywhere on the host into ``local_dir``.

    Each lands under its own base name. Returns ``(downloaded, skipped)``,
    where ``skipped`` pairs a remote path with the reason: not a file, a name
    that is not safe to write, two paths sharing a base name, or a local file
    already there and ``overwrite`` not given. A file the user has beside
    their work is not something a fetch should be able to replace silently.
    """
    os.makedirs(local_dir, exist_ok=True)
    downloaded: List[str] = []
    skipped: List[tuple] = []
    taken: set = set()
    for path in paths:
        name = safe_download_name(remote_paths.basename(str(path).rstrip("/\\")))
        if not name:
            skipped.append((path, "not a file name that is safe to write here"))
            continue
        if name in taken:
            skipped.append((path, f"another path in this request is also called {name}"))
            continue
        target = os.path.join(local_dir, name)
        if os.path.exists(target) and not overwrite:
            skipped.append((path, f"{target} already exists"))
            continue
        kind = stat_host_path(transport, host, path, digest=False)
        if kind.get("type") != "file":
            skipped.append(
                (path, "no such file on the host" if not kind["exists"] else "not a file")
            )
            continue
        taken.add(name)
        try:
            _download_atomic(transport, path, target, local_dir)
        except (TransportError, OSError) as exc:
            skipped.append((path, str(exc) or "the transfer failed"))
            continue
        downloaded.append(target)
    return downloaded, skipped


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

    directory = remote_runner.runner_dir(effective_root(host))
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
        statuses = _runner_statuses(transport, directory, finished)
        for job, outcome in zip(finished, outcomes):
            status = statuses.get(job.id, "")
            if status == remote_runner.STATUS_BLOCKED:
                # It never ran at all: the runner set it aside because what it
                # was waiting for failed, or was never queued.
                job.last_error = "Queued behind a job that did not succeed; it never started."
                outcome = STATE_FAILED
            elif outcome == STATE_LOST and status.lstrip("-").isdigit():
                # The wrapper's exit-code file is the first witness, and it can
                # be missing from this read while the job did finish: a
                # networked filesystem that has not shown it to this client
                # yet. The helper records the same code in its own directory,
                # and a finished job is not LOST while that one says otherwise.
                job.rc = int(status)
                outcome = STATE_DONE if job.rc == 0 else STATE_FAILED
            if outcome != job.state:
                updates[job.id] = outcome
    return updates


def _runner_statuses(transport: Transport, directory: str, jobs: Sequence[Job]) -> Dict[str, str]:
    """What the helper wrote in ``status/`` for each job: its exit code, or
    :data:`remote_runner.STATUS_BLOCKED` for one it set aside rather than ran.
    Empty for a job it has written nothing for."""
    speak = dialect.for_host(transport.host)
    paths = [remote_paths.join(directory, "status", job.remote_job_id) for job in jobs]
    result = transport.run(speak.read_files(paths, _SENTINEL_MARK))
    chunks = (result.stdout or "").split(_SENTINEL_MARK)[1:]
    statuses: Dict[str, str] = {}
    for index, job in enumerate(jobs):
        raw = chunks[index].strip() if index < len(chunks) else ""
        first = raw.splitlines()[0].strip() if raw.splitlines() else ""
        if first and first != dialect.MISSING:
            statuses[job.id] = first
    return statuses


def queue_paused(transport: Transport, host: HostProfile) -> bool:
    """Whether the host's runner is currently holding its queue."""
    directory = remote_runner.runner_dir(effective_root(host))
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
    directory = remote_runner.runner_dir(effective_root(host))
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
    """Ask the host what it has: ``(cores, memory_mb, threads)``, 0 where unknown.

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
    directory = remote_runner.runner_dir(effective_root(host))
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
    if not job.remote_job_id:
        # Never reached the queue: no entry to move and no pid to kill. Asked
        # for anyway when a submission is cancelled while it is still uploading.
        return
    directory = remote_runner.runner_dir(effective_root(host))
    transport.run(remote_runner.flavour_for(host).cancel_command(directory, job.remote_job_id))


def release_in_runner(transport: Transport, host: HostProfile, job: Job) -> None:
    """Let a queued job start although what it waits for did not succeed.

    Used when the user cancels one job of a chain and keeps the rest: without
    it the helper queue sets every job behind the cancelled one aside, which is
    the whole chain thrown away for one deliberate cancellation.
    """
    if not job.remote_job_id:
        return
    directory = remote_runner.runner_dir(effective_root(host))
    transport.run(remote_runner.flavour_for(host).release_command(directory, job.remote_job_id))


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


#: Files this plugin puts in the job directory itself. None of them is a
#: result: the wrapper's log holds whatever the command wrote to the terminal,
#: the sentinel holds one number, and the script is the script. Tail Log reads
#: the log on the host, which is where it belongs.
def is_plugin_file(name: str, log_file: str = "") -> bool:
    """True for a file this plugin wrote, rather than the calculation."""
    from .models import STARTED_NAME

    leaf = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if log_file and (name == log_file or leaf == f"{log_file}.err"):
        return True
    return (
        leaf.startswith(SENTINEL_NAME)
        or leaf.startswith(STARTED_NAME)
        or leaf.startswith("moleditpy_run.")
        or (leaf.startswith("moleditpy_") and leaf.endswith(".log"))
    )


#: Extensions a calculation writes, best first. The order is what decides which
#: file is offered when several could be opened.
RESULT_PRIORITY = (".out", ".log", ".fchk", ".hess", ".molden", ".cube", ".xyz")


def likely_outputs(names: Sequence[str], log_file: str = "") -> List[str]:
    """The files worth offering, in priority order; never the plugin's own.

    Used to preselect: the Open Result window opens on one of these, and the
    download chooser ticks them when the fetch patterns matched nothing at all
    -- which is the case it exists for. ``job.log`` is excluded however the
    ranking falls, because it is this plugin's file: the outputs the user cares
    about are the ones the command was told to write.
    """
    candidates = [name for name in names or [] if name and not is_plugin_file(name, log_file)]
    ranked: List[str] = []
    for extension in RESULT_PRIORITY:
        ranked += [name for name in candidates if name.lower().endswith(extension)]
    # Deduplicated, keeping the best rank each file reached.
    seen = set()
    ordered = []
    for name in ranked:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def primary_output(names: Sequence[str], log_file: str = "") -> str:
    """The one file to open, or "" when none of them looks like a result."""
    ranked = likely_outputs(names, log_file)
    return ranked[0] if ranked else ""


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
    protected = {os.path.realpath(path) for path in (job.input_files or []) if path}
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
        root = os.path.realpath(local_dir)
        if not _inside_directory(root, os.path.realpath(os.path.dirname(target))):
            logging.warning("Job Manager: refusing to write outside %s: %r", local_dir, name)
            continue
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.realpath(target) in protected:
            logging.debug("Job Manager: not overwriting the input file %s", name)
            continue
        # Into a part file, then renamed. Results land in the directory the
        # user is working in, so a transfer cut off half way would otherwise
        # leave a truncated .out sitting there under its real name, looking
        # exactly like a complete one -- and over the top of the previous
        # attempt's good copy.
        try:
            _download_atomic(transport, remote_paths.join(job.remote_dir, name), target, local_dir)
        except (TransportError, OSError):
            logging.warning("Job Manager: could not download %s", name)
            continue
        downloaded.append(target)
    return downloaded


def _inside_directory(root: str, path: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:
        # Windows junctions can point to a different drive.
        return False


def _download_atomic(transport: Transport, remote: str, target: str, root: str) -> None:
    """Stage in a private directory; never open a predictable existing link."""
    parent = os.path.realpath(os.path.dirname(target))
    base = os.path.realpath(root)
    if not _inside_directory(base, parent):
        raise TransportError("Download destination escapes the selected directory")
    destination = os.path.join(parent, os.path.basename(target))
    with tempfile.TemporaryDirectory(prefix=".moleditpy-download-", dir=parent) as staging_dir:
        staging = os.path.join(staging_dir, "result")
        transport.download(remote, staging)
        os.replace(staging, destination)


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
    "download_host_paths",
    "effective_root",
    "force_in_runner",
    "list_host_path",
    "recheck_job",
    "runner_queue",
    "stat_host_path",
    "fetch_results",
    "input_name_for",
    "is_plugin_file",
    "likely_outputs",
    "primary_output",
    "list_remote_files",
    "make_remote_dir",
    "name_job_files",
    "poll_host",
    "prepare_remote_dir",
    "require_remote_path",
    "script_name_for",
    "sentinel_for",
    "queue_paused",
    "release_in_runner",
    "select_files",
    "set_queue_paused",
    "short_id",
    "submit_job",
    "tail_log",
    "tail_remote_file",
]
