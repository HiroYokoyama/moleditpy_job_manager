"""PBS / Torque / OpenPBS: qsub / qstat / qdel."""

from __future__ import annotations

import re
import time
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

#: "12345.headnode" or plain "12345".
_JOB_ID_RE = re.compile(r"^(\d+(?:\[\])?(?:\.\S+)?)")

_STATE_MAP: Dict[str, str] = {
    "Q": STATE_PENDING,
    "W": STATE_PENDING,
    "H": STATE_PENDING,
    "T": STATE_PENDING,
    "S": STATE_PENDING,
    "M": STATE_PENDING,
    "R": STATE_RUNNING,
    "B": STATE_RUNNING,
    "E": STATE_COMPLETING,
    "C": STATE_COMPLETING,
    "F": STATE_COMPLETING,
    "X": STATE_COMPLETING,
}


class PbsScheduler(Scheduler):
    name = "pbs"
    label = "PBS / Torque"

    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        lines = [
            f"#PBS -N {job_name}",
            f"#PBS -o {log_file}",
            "#PBS -j oe",
        ]
        if preset.walltime:
            lines.append(f"#PBS -l walltime={preset.walltime}")
        nodes = int(preset.nodes or 1)
        ppn = int(preset.cpus_per_task or 1)
        if ppn > 1 or nodes > 1:
            lines.append(f"#PBS -l nodes={nodes}:ppn={ppn}")
        if preset.memory:
            lines.append(f"#PBS -l mem={preset.memory}")
        if preset.queue:
            lines.append(f"#PBS -q {preset.queue}")
        if preset.account:
            lines.append(f"#PBS -A {preset.account}")
        return lines

    def submit_command(self, script_name: str, log_file: str) -> str:
        return f"qsub {script_name}"

    def parse_submit_output(self, stdout: str, stderr: str) -> str:
        for line in (stdout or "").splitlines():
            match = _JOB_ID_RE.match(line.strip())
            if match:
                return match.group(1)
        return ""

    def status_command(self, username: str, job_ids: Iterable[str]) -> str:
        return f"qstat -u {username}"

    def parse_status(self, stdout: str) -> Dict[str, str]:
        states: Dict[str, str] = {}
        for line in (stdout or "").splitlines():
            stripped = line.strip()
            match = _JOB_ID_RE.match(stripped)
            if not match:
                continue
            parts = stripped.split()
            if len(parts) < 6:
                continue
            # `qstat -u` puts the one-letter state second from the right,
            # ahead of the elapsed-time column.
            job_id = match.group(1)
            states[job_id] = canonical_state(parts[-2], _STATE_MAP)
            states.setdefault(job_id.split(".")[0], states[job_id])
        return states

    def start_time_directives(self, start_after: float) -> List[str]:
        target = int(start_after or 0)
        if target <= 0:
            return []
        # PBS -a takes [[[[CC]YY]MM]DD]hhmm[.SS], not an ISO timestamp.
        return [f"#PBS -a {time.strftime('%Y%m%d%H%M.%S', time.localtime(target))}"]

    def dependency_directives(self, after_id: str, any_outcome: bool = False) -> List[str]:
        after_id = str(after_id or "").strip()
        if not after_id or not self.valid_job_id(after_id):
            return []
        kind = "afterany" if any_outcome else "afterok"
        return [f"#PBS -W depend={kind}:{after_id}"]

    def cancel_command(self, job_id: str) -> str:
        # Quoted: a job id is not always ours. One read from a job list file
        # would otherwise be a command the user's own account runs.
        return f"qdel {quote(job_id)}"


PBS = register(PbsScheduler())
