"""Shared teardown for the two runner harnesses.

Both flavours are driven the same way and have to be torn down the same way,
so the reaping lives here rather than in each test module (see CLAUDE.md: the
bash and PowerShell runners must not drift).
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Optional

#: What a host is given for ``command_timeout`` in a test that really starts a
#: process, rather than the production default of 60 s.
#:
#: The default is right for a user submitting a job: a handover that has taken
#: a minute is not going to finish, and saying so beats hanging. It is wrong for
#: CI, where the suite runs one xdist worker per logical core and a Windows
#: runner then has eight of them starting Windows PowerShell 5.1 at once --
#: which took over 60 s to hand back a pid and failed a submission test that had
#: nothing to do with timing. Nothing waits this out on the happy path; it is
#: only the point at which a test gives up rather than hanging for ever.
REAL_PROCESS_TIMEOUT = 300


def kill_dispatched_jobs(runner_dir: str, shell: Optional[str] = None) -> int:
    """Kill the jobs the runner started. Returns how many were signalled.

    A harness owns the runner processes it launched itself, and killing those
    was all the teardown did. But the runner launches the *queued jobs*, so
    they survived it -- and a job still holding its own directory made the
    following ``rmtree(ignore_errors=True)`` give up without a word. Both
    accumulated: eleven abandoned job processes, the oldest four days old, and
    ninety-five undeletable directories.

    The pids come from the runner's own ``pids/`` bookkeeping, which is what it
    uses to reap them, so this needs no guessing about process trees -- a job
    re-parented by a runner that has already exited is still recorded there.

    ``shell`` is the bash that wrote those pids, and the one that must signal
    them. Under Git Bash they are MSYS pids: after the runner's ``exec`` one
    names a Windows process that has already exited, whose number Windows then
    hands to something else. ``os.kill`` there is TerminateProcess on that
    number -- it killed an xdist worker mid-test and another test's job, which
    came back LOST. Bash's own ``kill`` resolves the MSYS pid.
    """
    pid_dir = os.path.join(runner_dir, "pids")
    if not os.path.isdir(pid_dir):
        return 0
    pids = []
    for name in os.listdir(pid_dir):
        try:
            with open(os.path.join(pid_dir, name), encoding="ascii") as handle:
                pid = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    if shell and pids:
        listed = " ".join(str(pid) for pid in pids)
        try:
            result = subprocess.run(
                [shell, "-c", f"for p in {listed}; do kill -TERM $p 2>/dev/null && echo $p; done"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0
        return len(result.stdout.split())
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            # Already gone, which is the ordinary case: the entry is only
            # removed once the runner has reaped it.
            continue
        killed += 1
    return killed


__all__ = ["kill_dispatched_jobs"]
