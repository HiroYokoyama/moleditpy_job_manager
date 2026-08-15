"""SLURM: sbatch / squeue / scancel."""

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

_JOB_ID_RE = re.compile(r"Submitted batch job (\d+)")

_STATE_MAP: Dict[str, str] = {
    "PENDING": STATE_PENDING,
    "PD": STATE_PENDING,
    "CONFIGURING": STATE_PENDING,
    "CF": STATE_PENDING,
    "REQUEUED": STATE_PENDING,
    "RQ": STATE_PENDING,
    "REQUEUE_HOLD": STATE_PENDING,
    "RESV_DEL_HOLD": STATE_PENDING,
    "SUSPENDED": STATE_PENDING,
    "S": STATE_PENDING,
    "RUNNING": STATE_RUNNING,
    "R": STATE_RUNNING,
    "COMPLETING": STATE_COMPLETING,
    "CG": STATE_COMPLETING,
    "STAGE_OUT": STATE_COMPLETING,
    "SO": STATE_COMPLETING,
}


class SlurmScheduler(Scheduler):
    name = "slurm"
    label = "SLURM"
    order = 30

    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        lines = [
            f"#SBATCH --job-name={job_name}",
            f"#SBATCH --output={log_file}",
            f"#SBATCH --error={log_file}",
        ]
        if preset.walltime:
            lines.append(f"#SBATCH --time={preset.walltime}")
        if preset.nodes:
            lines.append(f"#SBATCH --nodes={int(preset.nodes)}")
        if preset.ntasks:
            lines.append(f"#SBATCH --ntasks={int(preset.ntasks)}")
        if preset.cpus_per_task and int(preset.cpus_per_task) > 1:
            lines.append(f"#SBATCH --cpus-per-task={int(preset.cpus_per_task)}")
        if preset.memory:
            lines.append(f"#SBATCH --mem={preset.memory}")
        if preset.queue:
            lines.append(f"#SBATCH --partition={preset.queue}")
        if preset.account:
            lines.append(f"#SBATCH --account={preset.account}")
        return lines

    def submit_command(self, script_name: str, log_file: str) -> str:
        return f"sbatch --parsable {script_name}"

    def parse_submit_output(self, stdout: str, stderr: str) -> str:
        text = (stdout or "").strip()
        for line in text.splitlines():
            line = line.strip()
            # --parsable prints "jobid" or "jobid;cluster".
            candidate = line.split(";")[0].strip()
            if candidate.isdigit():
                return candidate
        match = _JOB_ID_RE.search(f"{stdout}\n{stderr}")
        return match.group(1) if match else ""

    def status_command(self, username: str, job_ids: Iterable[str]) -> str:
        return f'squeue -h -u {username} -o "%i %T"'

    def parse_status(self, stdout: str) -> Dict[str, str]:
        states: Dict[str, str] = {}
        for line in (stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            job_id = parts[0].strip()
            # Array tasks appear as 123_4; track them under the parent id too.
            states[job_id] = canonical_state(parts[1], _STATE_MAP)
            if "_" in job_id:
                states.setdefault(job_id.split("_")[0], states[job_id])
        return states

    def start_time_directives(self, start_after: float) -> List[str]:
        target = int(start_after or 0)
        if target <= 0:
            return []
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(target))
        return [f"#SBATCH --begin={stamp}"]

    def dependency_directives(self, after_id: str, any_outcome: bool = False) -> List[str]:
        after_id = str(after_id or "").strip()
        if not after_id or not self.valid_job_id(after_id):
            return []
        kind = "afterany" if any_outcome else "afterok"
        return [f"#SBATCH --dependency={kind}:{after_id}"]

    def cancel_command(self, job_id: str) -> str:
        # Quoted: a job id is not always ours. One read from a job list file
        # would otherwise be a command the user's own account runs.
        return f"scancel {quote(job_id)}"


SLURM = register(SlurmScheduler())
