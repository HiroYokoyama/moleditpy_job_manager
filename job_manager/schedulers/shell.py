"""No scheduler: run the script under ``nohup`` and track it by PID.

For group workstations and login-node-only machines that have no queue. The
"job id" is the background process id, and liveness is one ``ps`` call for all
tracked pids at once.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

from ..models import SubmitPreset
from .base import STATE_RUNNING, Scheduler, register


class ShellScheduler(Scheduler):
    name = "shell"
    label = "None (background process)"

    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        return [f"# job: {job_name}"]

    def submit_command(self, script_name: str, log_file: str) -> str:
        return (
            f"chmod +x {script_name} && "
            f"nohup bash {script_name} > {log_file} 2>&1 < /dev/null & echo $!"
        )

    def parse_submit_output(self, stdout: str, stderr: str) -> str:
        for line in reversed((stdout or "").splitlines()):
            token = line.strip()
            if token.isdigit():
                return token
        return ""

    def status_command(self, username: str, job_ids: Iterable[str]) -> str:
        pids = [str(j).strip() for j in job_ids if str(j).strip().isdigit()]
        if not pids:
            # Nothing to ask about; keep the contract of returning a command.
            return "true"
        return f"ps -o pid= -p {','.join(pids)}"

    def parse_status(self, stdout: str) -> Dict[str, str]:
        states: Dict[str, str] = {}
        for line in (stdout or "").splitlines():
            token = line.strip()
            if token.isdigit():
                states[token] = STATE_RUNNING
        return states

    def cancel_command(self, job_id: str) -> str:
        # Kill the whole process group so the payload dies with the wrapper.
        return f"kill -- -$(ps -o pgid= -p {job_id} | tr -d ' ') 2>/dev/null || kill {job_id}"


SHELL = register(ShellScheduler())
