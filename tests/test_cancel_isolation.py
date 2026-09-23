"""Cancelling one no-queue job must not touch anything else.

A shell with no job control puts what it backgrounds in its own process group,
so every job the helper queue started shared one group with the runner -- and,
on a local host, with MoleditPy itself. Cancel killed that whole group. These
run for real, on a POSIX machine with ``setsid`` and a ``ps`` that reads
``-o pgid=``; the command text is covered elsewhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from job_manager import remote_runner
from job_manager.schedulers import get_scheduler

BASH = shutil.which("bash") if os.name == "posix" else None


def _pgid_readable() -> bool:
    if not BASH or not shutil.which("setsid"):
        return False
    probe = subprocess.run(
        [BASH, "-c", "ps -o pgid= -p $$"], capture_output=True, text=True, timeout=10
    )
    return probe.stdout.strip().isdigit()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A zombie still answers kill -0; ask ps whether it has really gone.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(_pgid_readable(), "needs bash, setsid and ps -o pgid")
class TestCancelKillsOnlyItsOwnJob(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="cancel_isolation_")
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.pids = []
        self.addCleanup(self._kill_leftovers)

    def _kill_leftovers(self):
        for pid in self.pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def _submit(self, name: str, command: str) -> int:
        with open(os.path.join(self.workdir, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/bash\nsleep 60\n")
        result = subprocess.run(
            [BASH, "-c", f"cd {self.workdir} && {command}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        pid = int(result.stdout.strip().splitlines()[-1])
        self.pids.append(pid)
        return pid

    def _cancel(self, command: str) -> None:
        subprocess.run([BASH, "-c", command], capture_output=True, timeout=30)

    def test_the_job_beside_it_keeps_running(self):
        shell = get_scheduler("shell")
        first = self._submit("a.sh", shell.submit_command("a.sh", "a.log"))
        second = self._submit("b.sh", shell.submit_command("b.sh", "b.log"))

        self._cancel(shell.cancel_command(str(first)))

        self.assertTrue(_wait_dead(first), "the cancelled job is still running")
        self.assertTrue(_alive(second), "cancelling one job killed the other")

    def test_a_job_sharing_a_group_is_killed_alone(self):
        # Started the way older versions started it: no group of its own. The
        # group it shares here is the one running these tests.
        legacy = "{ nohup bash a.sh > a.log 2>&1 < /dev/null & } && echo $!"
        first = self._submit("a.sh", legacy)
        second = self._submit("b.sh", legacy.replace("a.", "b."))

        self._cancel(remote_runner.kill_job_command(str(first)))

        self.assertTrue(_wait_dead(first), "the cancelled job is still running")
        self.assertTrue(_alive(second), "cancelling one job killed the other")


if __name__ == "__main__":
    unittest.main()
