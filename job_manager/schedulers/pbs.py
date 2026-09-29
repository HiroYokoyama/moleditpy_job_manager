"""PBS / Torque / OpenPBS: qsub / qstat / qdel."""

from __future__ import annotations

import re
import time
from typing import Dict, Iterable, List, Sequence

from ..models import SubmitPreset
from ..remote_paths import quote
from .base import (
    STATE_COMPLETING,
    STATE_PENDING,
    STATE_RUNNING,
    Scheduler,
    canonical_state,
    memory_request,
    register,
    user_argument,
)

#: "12345.headnode" or plain "12345".
_JOB_ID_RE = re.compile(r"^(\d+(?:\[\])?(?:\.\S+)?)")
#: What qsub prints on success, alone on its line.
_SUBMITTED_RE = re.compile(r"\d+(?:\[\d*\])?(?:\.[A-Za-z0-9._-]+)?")

#: PBS spells sizes in lower-case bytes-words, and reads a bare number as bytes.
_MEMORY_UNITS = {"M": "mb", "G": "gb", "T": "tb"}

#: Finished, not finishing. Torque keeps a completed job in ``qstat`` as ``C``
#: for ``keep_completed`` seconds -- often minutes, on some sites hours -- and
#: PBS Pro lists ``F``/``X`` under ``-x``. The wrapper has already written its
#: exit code by then, so such a row is read as "gone from the queue" and the
#: sentinel decides; kept as COMPLETING, the job sat there for the whole window.
_FINISHED = frozenset({"C", "F", "X"})

#: PBS Pro before 19 and some Torque builds refuse a longer ``-N``.
_NAME_LIMIT = 15

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


def queue_job_name(job_name: str) -> str:
    """``job_name`` as PBS and SGE accept it for ``-N``.

    Both want a letter first -- a molecule named ``2-butanol`` was refused at
    submission -- and older PBS a short name. Only the queue's label changes;
    ``{name}`` in the command still gets the whole name.
    """
    name = job_name or "job"
    if not name[0].isalpha():
        name = f"j{name}"
    return name[:_NAME_LIMIT]


class PbsScheduler(Scheduler):
    name = "pbs"
    label = "PBS / Torque"
    order = 40

    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        lines = [
            f"#PBS -N {queue_job_name(job_name)}",
            f"#PBS -o {log_file}",
            "#PBS -j oe",
            # Without it the script is run by the user's *login* shell: the
            # shebang is ignored, and on an account whose shell is tcsh every
            # bash line in the wrapper -- the traps included -- is a syntax
            # error, so the job dies before the sentinel exists and reads LOST.
            "#PBS -S /bin/bash",
        ]
        if preset.walltime:
            lines.append(f"#PBS -l walltime={preset.walltime}")
        nodes = max(1, int(preset.nodes or 1))
        # ppn is per node and counts every core there: MPI ranks times the
        # threads each one runs. Reading cpus_per_task alone gave a four-rank
        # job one core, and it then ran four ranks on it.
        ranks_per_node = -(-max(1, int(preset.ntasks or 1)) // nodes)
        ppn = ranks_per_node * max(1, int(preset.cpus_per_task or 1))
        if ppn > 1 or nodes > 1:
            lines.append(f"#PBS -l nodes={nodes}:ppn={ppn}")
        if preset.memory:
            lines.append(f"#PBS -l mem={memory_request(preset.memory, _MEMORY_UNITS, 'M')}")
        if preset.queue:
            lines.append(f"#PBS -q {preset.queue}")
        if preset.account:
            lines.append(f"#PBS -A {preset.account}")
        return lines

    def submit_command(
        self, script_name: str, log_file: str, extra_args: Sequence[str] = ()
    ) -> str:
        # Quoted for the reason cancel_command gives: safe_relative_name
        # permits spaces and semicolons, and this string is run by a shell.
        return " ".join(["qsub", *extra_args, quote(script_name)])

    def parse_submit_output(self, stdout: str, stderr: str) -> str:
        # Last line first, and the whole line: the login files the job's
        # environment reads can print a banner before qsub says anything, and
        # a first-match on "2026-10-01 maintenance" recorded job 2026.
        for line in reversed((stdout or "").splitlines()):
            if _SUBMITTED_RE.fullmatch(line.strip()):
                return line.strip()
        return ""

    def status_command(self, username: str, job_ids: Iterable[str]) -> str:
        return f"qstat -u {user_argument(username)}"

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
            if parts[-2].upper() in _FINISHED:
                continue
            states[job_id] = canonical_state(parts[-2], _STATE_MAP)
            states.setdefault(job_id.split(".")[0], states[job_id])
        return states

    def start_time_directives(self, start_after: float) -> List[str]:
        target = int(start_after or 0)
        if target <= 0:
            return []
        # PBS -a takes [[[[CC]YY]MM]DD]hhmm[.SS], not an ISO timestamp.
        #
        # Known limitation: that stamp carries no timezone and the server reads
        # it in *its* local time, so a start time chosen here lands wrong by the
        # difference between the two clocks. SLURM avoids this with `now+N`;
        # PBS has no relative form, and the only correct fix is to learn the
        # host's UTC offset at submit time -- a round trip this deliberately
        # does not spend without being asked. Writing the local stamp is what
        # a user typing `qsub -a` by hand would get, so it is at least the
        # behaviour the site's own documentation describes.
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
