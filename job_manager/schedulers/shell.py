"""No scheduler: run the script under ``nohup`` and track it by PID.

For group workstations and login-node-only machines that have no queue. The
"job id" is the background process id, and liveness is one ``ps`` call for all
tracked pids at once.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

from ..models import SubmitPreset
from ..remote_paths import quote
from .base import STATE_RUNNING, Scheduler, register


class ShellScheduler(Scheduler):
    name = "shell"
    label = "None (background process)"
    # Nothing else serialises work on a machine with no queue, so this is the
    # one scheduler where "run after that job" has to be arranged by hand.
    supports_chaining = True

    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        return [f"# job: {job_name}"]

    def submit_command(self, script_name: str, log_file: str) -> str:
        # The braces matter. Written as `A && nohup B ... & echo $!`, the `&`
        # backgrounds the whole `&&` list, and that subshell keeps the caller's
        # stdout and stderr open for as long as the job runs -- so submitting
        # blocked until the calculation finished, holding an ssh connection and
        # a worker thread the entire time. Backgrounding only the nohup, whose
        # three streams all go elsewhere, lets the shell exit at once.
        # It also makes $! the wrapper's own pid rather than a subshell's.
        return (
            f"chmod +x {script_name} && "
            f"{{ nohup bash {script_name} > {log_file} 2>&1 < /dev/null & }} && echo $!"
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
        # `kill -0` is a shell builtin: no ps, so this also works where ps is
        # restricted (hidepid) or cut down (busybox).
        checks = " ".join(pids)
        return f"for p in {checks}; do kill -0 $p 2>/dev/null && echo $p; done; true"

    def parse_status(self, stdout: str) -> Dict[str, str]:
        states: Dict[str, str] = {}
        for line in (stdout or "").splitlines():
            token = line.strip()
            if token.isdigit():
                states[token] = STATE_RUNNING
        return states

    def cancel_command(self, job_id: str) -> str:
        # Kill the whole process group so the payload dies with the wrapper.
        # The pid is quoted: a job list can come from anywhere, and this string
        # is executed by the user's shell on the remote machine.
        pid = quote(job_id)
        return f"kill -- -$(ps -o pgid= -p {pid} | tr -d ' ') 2>/dev/null || kill {pid}"


SHELL = register(ShellScheduler())
