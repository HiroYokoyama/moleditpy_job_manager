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

#: Queue reported something we do not recognise; the poller falls back to the
#: sentinel file rather than guessing.
STATE_UNKNOWN = "UNKNOWN"

#: How often a chained job checks whether its predecessor has exited. Long
#: enough to cost nothing (kill -0 is a shell builtin), short enough that a
#: queue of short jobs still moves briskly.
CHAIN_POLL_SECONDS = 5


#: Both spellings are accepted: ``{input}`` and ``[input]``. Square brackets
#: need no shell escaping and do not collide with brace expansion.
_BRACE_RE = re.compile(r"\{(\w+)\}")
_SQUARE_RE = re.compile(r"\[(\w+)\]")


def placeholder_values(input_name: str, preset: SubmitPreset) -> Dict[str, str]:
    """Every tag a command template may use, resolved for this job."""
    stem = posixpath.splitext(input_name)[0] if input_name else ""
    return {
        "input": input_name,
        "basename": input_name,
        "stem": stem,
        "output": f"{stem}.out" if stem else "",
        "nodes": preset.nodes,
        "ntasks": preset.ntasks,
        "cpus": preset.cpus_per_task,
        "cpus_per_task": preset.cpus_per_task,
        "memory": preset.memory,
        "walltime": preset.walltime,
        "queue": preset.queue,
    }


def format_command(template: str, input_name: str, preset: SubmitPreset) -> str:
    """Substitute the placeholders a command template may use.

    Only *known* tags are touched, one at a time, rather than handing the whole
    string to ``str.format``: a command containing shell braces or brackets --
    ``awk '{print $1}'``, ``if [ -f x ]`` -- made format() raise, and the old
    fallback then ran the template with nothing substituted at all.
    """
    values = placeholder_values(input_name, preset)

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

    #: A queue already serialises work, so only the no-queue scheduler needs
    #: the wrapper to wait for its predecessor itself.
    supports_chaining: bool = False

    def build_script(
        self,
        job_name: str,
        preset: SubmitPreset,
        input_name: str,
        log_file: str,
        wait_for_pid: str = "",
    ) -> str:
        """Assemble the complete run script, sentinel included."""
        lines: List[str] = ["#!/bin/bash"]
        lines += self.directives(sanitize_name(job_name), preset, log_file)
        lines += [directive for directive in (preset.extra_directives or []) if directive.strip()]
        lines += [
            "",
            'cd "$(dirname "$0")" || exit 1',
            f"rm -f {SENTINEL_NAME}",
            # An EXIT trap, not a trailing echo: a payload that calls `exit`
            # itself (or a pre-command that fails under `set -e`) would never
            # reach a trailing line, and the job would look LOST rather than
            # FAILED. The trap's $? is the real final status either way.
            f"trap '__moleditpy_rc=$?; echo \"$__moleditpy_rc\" > {SENTINEL_NAME}' EXIT",
            # Without these, a job the scheduler kills -- walltime exceeded,
            # preemption, scancel, node drain -- reaches the EXIT trap with $?
            # still 0 and is recorded as a clean success. Each killing signal
            # is turned into its conventional 128+n status first.
            "trap 'exit 143' TERM",
            "trap 'exit 130' INT",
            "trap 'exit 129' HUP",
            "",
        ]
        lines += self._wait_block(wait_for_pid)
        for module in preset.modules or []:
            if module.strip():
                lines.append(f"module load {module.strip()}")
        for command in preset.pre_commands or []:
            if command.strip():
                lines.append(command.strip())
        lines += [
            "",
            format_command(preset.command_template, input_name, preset),
            "",
        ]
        return "\n".join(lines)

    def _wait_block(self, wait_for_pid: str) -> List[str]:
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
        pid = str(wait_for_pid or "").strip()
        if not pid.isdigit():
            return []
        return [
            f"# Chained: wait for job {pid} on this machine to finish first.",
            f"while kill -0 {pid} 2>/dev/null; do sleep {CHAIN_POLL_SECONDS}; done",
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
