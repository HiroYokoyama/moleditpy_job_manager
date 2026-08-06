"""SGE / UGE / Grid Engine: qsub / qstat / qdel.

Same verbs as PBS but a different directive prefix and a different ``qstat``
layout, so it gets its own parser.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

from ..models import SubmitPreset
from ..remote_paths import quote
from .base import (
    STATE_COMPLETING,
    STATE_PENDING,
    STATE_RUNNING,
    Scheduler,
    canonical_state,
    register,
)

_SUBMIT_RE = re.compile(r"[Yy]our job(?:-array)?\s+(\d+)")

_STATE_MAP: Dict[str, str] = {
    "QW": STATE_PENDING,
    "W": STATE_PENDING,
    "HQW": STATE_PENDING,
    "HRWQ": STATE_PENDING,
    "H": STATE_PENDING,
    "S": STATE_PENDING,
    "TS": STATE_PENDING,
    # Error states stay queued until an operator clears them; report them as
    # pending rather than inventing a terminal state the queue never reached.
    "EQW": STATE_PENDING,
    "EHQW": STATE_PENDING,
    "R": STATE_RUNNING,
    "T": STATE_RUNNING,
    "RR": STATE_RUNNING,
    "RT": STATE_RUNNING,
    "D": STATE_COMPLETING,
    "DR": STATE_COMPLETING,
    "DT": STATE_COMPLETING,
}


class SgeScheduler(Scheduler):
    name = "sge"
    label = "SGE / UGE"

    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        lines = [
            f"#$ -N {job_name}",
            f"#$ -o {log_file}",
            "#$ -j y",
            "#$ -cwd",
            "#$ -S /bin/bash",
        ]
        if preset.walltime:
            lines.append(f"#$ -l h_rt={preset.walltime}")
        if preset.memory:
            lines.append(f"#$ -l h_vmem={preset.memory}")
        if preset.queue:
            lines.append(f"#$ -q {preset.queue}")
        if preset.account:
            lines.append(f"#$ -A {preset.account}")
        # The parallel environment name is site-specific; anything beyond a
        # plain serial job belongs in the preset's extra directives.
        return lines

    def submit_command(self, script_name: str, log_file: str) -> str:
        return f"qsub {script_name}"

    def parse_submit_output(self, stdout: str, stderr: str) -> str:
        match = _SUBMIT_RE.search(f"{stdout}\n{stderr}")
        return match.group(1) if match else ""

    def status_command(self, username: str, job_ids: Iterable[str]) -> str:
        return f"qstat -u {username}"

    def parse_status(self, stdout: str) -> Dict[str, str]:
        states: Dict[str, str] = {}
        for line in (stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 5 or not parts[0].isdigit():
                continue
            # job-ID prior name user state ...
            states[parts[0]] = canonical_state(parts[4], _STATE_MAP)
        return states

    def cancel_command(self, job_id: str) -> str:
        # Quoted: a job id is not always ours. One read from a job list file
        # would otherwise be a command the user's own account runs.
        return f"qdel {quote(job_id)}"


SGE = register(SgeScheduler())
