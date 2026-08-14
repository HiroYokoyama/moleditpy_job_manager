"""Scheduler abstraction and run-script generation.

Every queue system differs in three places -- the directive block, the submit
verb, and the status output format -- so that is exactly what a subclass
supplies. Completion, by contrast, is handled identically everywhere:

The generated script ends by writing the payload's exit code to
``.moleditpy_rc``. A job that has disappeared from the queue is then resolved
by reading that one file: present means finished (with its real exit code),
absent means the job was killed before the payload returned. That avoids
depending on ``sacct`` (frequently disabled) or on parsing site-specific
``qstat -f`` output.
"""

from __future__ import annotations

import posixpath
import re
import time
from abc import ABC, abstractmethod
from typing import Dict, Iterable, List

from ..models import (
    SENTINEL_NAME,
    STARTED_NAME,
    STATE_COMPLETING,
    STATE_PENDING,
    STATE_RUNNING,
    SubmitPreset,
    sanitize_name,
)
from ..remote_paths import quote

# One spelling of the core request, shared with the helper queue that reads it.
# Importing it the other way round would be a cycle: the runner modules build
# on the schedulers, not the reverse.
from ..remote_runner import CORES_TAG, MEMORY_TAG

#: Multipliers for the suffixes a resource request is written with. Case is
#: ignored, and a bare number is already megabytes -- the unit every queue
#: system defaults to.
_MEMORY_UNITS = {"": 1, "M": 1, "MB": 1, "G": 1024, "GB": 1024, "T": 1024 * 1024, "TB": 1024 * 1024}
_MEMORY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$")


def parse_memory_mb(text: str) -> int:
    """``"8G"`` -> 8192. 0 for anything that is not a size.

    Users write memory the way their cluster spells it -- ``8G``, ``8GB``,
    ``8192``, ``512M`` -- and the helper needs one unit to do arithmetic in.
    Anything unparseable means "no request", never a wrong number: a job that
    asks for nothing waits for nothing, which is the safe direction.
    """
    match = _MEMORY_RE.match(str(text or ""))
    if not match:
        return 0
    unit = _MEMORY_UNITS.get(match.group(2).upper())
    if unit is None:
        return 0
    return int(float(match.group(1)) * unit)


def requested_memory_mb(preset) -> int:
    """Megabytes this job asks for, or 0 when it asks for none."""
    return parse_memory_mb(getattr(preset, "memory", "") or "")


def requested_cores(preset) -> int:
    """How many CPU cores this job asks for.

    The helper queue schedules on this number: a job starts when that many
    cores are free, so it is the request that decides what runs alongside what
    on a machine with no queue of its own.
    """
    return max(1, int(getattr(preset, "cpus_per_task", 1) or 1))


#: Queue reported something we do not recognise; the poller falls back to the
#: sentinel file rather than guessing.
STATE_UNKNOWN = "UNKNOWN"

#: How often a waiting wrapper looks again -- at its predecessor's process, or
#: at the clock. Long enough to cost nothing (both checks are shell builtins),
#: short enough that a queue of short jobs still moves briskly.
WAIT_POLL_SECONDS = 5

#: Job ids a queue will accept: digits, array suffixes (123_4, 123[]), and the
#: host suffix PBS appends (123.head.cluster). Anything else is not put into a
#: directive line -- a queue silently ignores a directive it cannot parse, and
#: a silently ignored dependency means the jobs run in the wrong order.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.\[\]:-]+$")


#: Both spellings are accepted: ``{input}`` and ``[input]``. Square brackets
#: need no shell escaping and do not collide with brace expansion.
_BRACE_RE = re.compile(r"\{(\w+)\}")
_SQUARE_RE = re.compile(r"\[(\w+)\]")


def placeholder_values(
    input_name: str, preset: SubmitPreset, job_name: str = "", remote_dir: str = ""
) -> Dict[str, str]:
    """Every tag a command template may use, resolved for this job.

    ``name`` and ``jobdir`` are what a job with no input file at all has to
    work with: a command that runs over a directory already prepared on the
    host names it, rather than a file this plugin uploaded.
    """
    stem = posixpath.splitext(input_name)[0] if input_name else ""
    return {
        "input": input_name,
        "basename": input_name,
        "stem": stem,
        "output": f"{stem}.out" if stem else "",
        "name": job_name,
        "jobdir": remote_dir,
        "nodes": preset.nodes,
        "ntasks": preset.ntasks,
        "cpus": preset.cpus_per_task,
        "cpus_per_task": preset.cpus_per_task,
        "memory": preset.memory,
        "walltime": preset.walltime,
        "queue": preset.queue,
    }


def format_command(
    template: str,
    input_name: str,
    preset: SubmitPreset,
    job_name: str = "",
    remote_dir: str = "",
) -> str:
    """Substitute the placeholders a command template may use.

    Only *known* tags are touched, one at a time, rather than handing the whole
    string to ``str.format``: a command containing shell braces or brackets --
    ``awk '{print $1}'``, ``if [ -f x ]`` -- made format() raise, and the old
    fallback then ran the template with nothing substituted at all.
    """
    values = placeholder_values(input_name, preset, job_name, remote_dir)

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    text = _BRACE_RE.sub(replace, template or "")
    return _SQUARE_RE.sub(replace, text)


class Scheduler(ABC):
    """Queue-system specific command construction and output parsing."""

    #: Registry key, matching ``HostProfile.scheduler``.
    name: str = ""
    #: Human label for the UI.
    label: str = ""
    #: File name of the generated submit script.
    script_name: str = "moleditpy_run.sh"

    # --- script -------------------------------------------------------------

    @abstractmethod
    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        """The ``#SBATCH`` / ``#PBS`` / ``#$`` block, without the shebang."""

    #: Every scheduler can run one job after another; only the mechanism
    #: differs. A queue takes a directive, the no-queue mode has the wrapper
    #: wait for the process itself.
    supports_chaining: bool = True

    #: True when a failed predecessor still releases the jobs behind it. SGE
    #: holds on completion and the wrapper's ``kill -0`` stops testing true the
    #: moment the process is gone, so for those two a failure costs nothing.
    #: SLURM and PBS use ``afterok``, where one failure strands the whole rest
    #: of the chain -- which is why :meth:`dependency_directives` can be asked
    #: for the ``afterany`` form instead.
    chain_releases_on_failure: bool = False

    def build_script(
        self,
        job_name: str,
        preset: SubmitPreset,
        input_name: str,
        log_file: str,
        run_after: str = "",
        start_after: float = 0.0,
        remote_dir: str = "",
        run_after_any: bool = False,
        sentinel: str = SENTINEL_NAME,
    ) -> str:
        """Assemble the complete run script, sentinel included.

        ``sentinel`` is a parameter because a job running in a directory the
        user prepared shares it: with one fixed name, two such jobs overwrite
        each other's exit code and whichever finished first decides what both
        are reported to have done.
        """
        sentinel = sentinel or SENTINEL_NAME
        lines: List[str] = ["#!/bin/bash"]
        lines += self.directives(sanitize_name(job_name), preset, log_file)
        dependency = self.dependency_directives(run_after, any_outcome=run_after_any)
        start_time = self.start_time_directives(start_after)
        lines += dependency
        lines += start_time
        lines += [directive for directive in (preset.extra_directives or []) if directive.strip()]
        lines += [
            "",
            self.cd_to_job_dir(remote_dir),
            f"rm -f {quote(sentinel)}",
            # An EXIT trap, not a trailing echo: a payload that calls `exit`
            # itself (or a pre-command that fails under `set -e`) would never
            # reach a trailing line, and the job would look LOST rather than
            # FAILED. The trap's $? is the real final status either way.
            # Written beside itself and renamed, never straight into place:
            # `>` truncates first, so a poll landing between the truncation and
            # the write reads an empty file -- which the reading side cannot
            # tell from a missing one, and reports a finished job as LOST.
            f'trap \'__moleditpy_rc=$?; echo "$__moleditpy_rc" > {quote(sentinel + ".tmp")}'
            f" && mv -f {quote(sentinel + '.tmp')} {quote(sentinel)}' EXIT",
            # Without these, a job the scheduler kills -- walltime exceeded,
            # preemption, scancel, node drain -- reaches the EXIT trap with $?
            # still 0 and is recorded as a clean success. Each killing signal
            # is turned into its conventional 128+n status first.
            "trap 'exit 143' TERM",
            "trap 'exit 130' INT",
            "trap 'exit 129' HUP",
            "",
        ]
        # Only wait in the wrapper for what the queue was not asked to wait for.
        # Emitting both put `while kill -0 <queue job id>` into a queue script,
        # where that number is a pid on the compute node belonging to some
        # unrelated process -- and the job then span in `sleep` until either
        # that process exited or the walltime ran out.
        if not start_time:
            lines += self._start_time_block(start_after)
        if not dependency:
            lines += self._predecessor_wait_block(run_after)
        for module in preset.modules or []:
            if module.strip():
                lines.append(f"module load {module.strip()}")
        for command in preset.pre_commands or []:
            if command.strip():
                lines.append(command.strip())
        lines += [
            "",
            format_command(preset.command_template, input_name, preset, job_name, remote_dir),
            "",
        ]
        return "\n".join(lines)

    #: Fallbacks for a script built without a known job directory (the wizard's
    #: preview). Each queue exports the directory the job was submitted from.
    _SUBMIT_DIR_FALLBACK = '"${SLURM_SUBMIT_DIR:-${PBS_O_WORKDIR:-$(dirname "$0")}}"'

    def cd_to_job_dir(self, remote_dir: str = "") -> str:
        """The line that puts the script in the directory holding the input.

        ``dirname "$0"`` is wrong for every real queue: sbatch and qsub run a
        *copy* of the script from their own spool directory, so ``$0`` points
        there and not at the uploaded input. The payload then could not find
        its input, and the sentinel was written into a spool directory that is
        deleted with the job -- which the poller reads as LOST. The job
        directory is known at submit time, so it is baked in instead.
        """
        return f"cd {quote(remote_dir) if remote_dir else self._SUBMIT_DIR_FALLBACK} || exit 1"

    def dependency_directives(self, after_id: str, any_outcome: bool = False) -> List[str]:
        """Directive lines telling the queue to hold this job until ``after_id``.

        ``any_outcome`` asks for the dependency to be satisfied by the
        predecessor *ending* rather than by it succeeding -- the difference
        between a chain that survives one failed calculation and one that
        strands everything behind it.

        Empty for the no-queue scheduler, which waits in the wrapper instead.
        Directives have to precede the first executable line or the queue never
        reads them, which :meth:`build_script` guarantees by placing them with
        the rest of the directive block.
        """
        return []

    def start_time_directives(self, start_after: float) -> List[str]:
        """Directive lines telling the queue to hold this job until a time.

        Empty for the no-queue scheduler, which sleeps in the wrapper instead.
        """
        return []

    def _start_time_block(self, start_after: float) -> List[str]:
        """Sleep until the requested time, for a scheduler that cannot wait.

        Compares epoch seconds so a clock in another timezone, or a machine
        whose idea of "now" differs from this one's, still starts the job at
        the instant the user meant.
        """
        target = int(start_after or 0)
        if target <= 0:
            return []
        return [
            f"# Scheduled: hold until epoch {target}"
            f" ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(target))} here).",
            f'while [ "$(date +%s)" -lt {target} ]; do sleep {WAIT_POLL_SECONDS}; done',
            "",
        ]

    @staticmethod
    def valid_job_id(job_id: str) -> bool:
        return bool(_JOB_ID_RE.match(str(job_id or "").strip()))

    def _predecessor_wait_block(self, run_after: str) -> List[str]:
        """Hold the job until the process it was chained behind has exited.

        The waiting happens on the remote machine, not in the plugin: a queue
        held on this side would stall the moment MoleditPy is closed, while a
        wrapper that waits for itself keeps the chain running over a lunch
        break, a reboot, or a lost network.

        ``kill -0`` is a shell builtin and needs no ``ps``, so this also works
        where the process table is restricted. A predecessor that is already
        gone -- finished, killed, never started -- fails the test immediately
        and the job simply runs.
        """
        pid = str(run_after or "").strip()
        if not pid.isdigit():
            return []
        return [
            f"# Chained: wait for job {pid} on this machine to finish first.",
            f"while kill -0 {pid} 2>/dev/null; do sleep {WAIT_POLL_SECONDS}; done",
            f"touch {STARTED_NAME}",
            "",
        ]

    # --- submit -------------------------------------------------------------

    @abstractmethod
    def submit_command(self, script_name: str, log_file: str) -> str:
        """Command run inside the job directory to enqueue the script."""

    @abstractmethod
    def parse_submit_output(self, stdout: str, stderr: str) -> str:
        """Extract the queue's job identifier from the submit output."""

    # --- status -------------------------------------------------------------

    @abstractmethod
    def status_command(self, username: str, job_ids: Iterable[str]) -> str:
        """One command covering *every* active job on the host."""

    @abstractmethod
    def parse_status(self, stdout: str) -> Dict[str, str]:
        """Map queue job id -> canonical state for everything still queued."""

    @abstractmethod
    def cancel_command(self, job_id: str) -> str:
        """Command that removes a job from the queue."""


_REGISTRY: Dict[str, Scheduler] = {}


def register(scheduler: Scheduler) -> Scheduler:
    _REGISTRY[scheduler.name] = scheduler
    return scheduler


def get_scheduler(name: str) -> Scheduler:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"Unknown scheduler: {name!r}") from None


def available_schedulers() -> List[Scheduler]:
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def canonical_state(raw: str, mapping: Dict[str, str]) -> str:
    """Look a scheduler's own state code up in its mapping table."""
    token = (raw or "").strip().upper()
    if not token:
        return STATE_UNKNOWN
    if token in mapping:
        return mapping[token]
    # SLURM appends a reason in parentheses, PBS sometimes a suffix.
    head = token.split("(")[0].strip()
    return mapping.get(head, STATE_UNKNOWN)


__all__ = [
    "CORES_TAG",
    "MEMORY_TAG",
    "parse_memory_mb",
    "requested_cores",
    "requested_memory_mb",
    "Scheduler",
    "STATE_UNKNOWN",
    "STATE_PENDING",
    "STATE_RUNNING",
    "STATE_COMPLETING",
    "available_schedulers",
    "canonical_state",
    "format_command",
    "placeholder_values",
    "get_scheduler",
    "register",
]
