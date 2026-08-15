"""Run generated scripts through a real bash.

Every other test asserts on the *text* of the script. These execute it, which is
the only way to catch semantics the text looks fine for: the original trailing
``echo $? > .moleditpy_rc`` was never reached by a payload that called ``exit``
itself, so a failed job was reported LOST instead of FAILED.

Skipped where no bash exists (which includes the pytest-only CI job on a runner
that does have one, so this actually runs there).
"""

import os
import subprocess
import tempfile
import time
import unittest

from job_manager.models import SENTINEL_NAME, SubmitPreset
from job_manager.schedulers import get_scheduler

from .bash_support import find_bash

BASH = find_bash()


@unittest.skipUnless(BASH, "no bash available")
class TestGeneratedScriptSemantics(unittest.TestCase):
    def run_script(self, command_template, scheduler_name="shell"):
        workdir = tempfile.mkdtemp(prefix="script_exec_")
        script = get_scheduler(scheduler_name).build_script(
            "t", SubmitPreset(command_template=command_template), "mol.inp", "job.log"
        )
        path = os.path.join(workdir, "run.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        proc = subprocess.run([BASH, path], capture_output=True, text=True, timeout=60, cwd=workdir)
        sentinel = os.path.join(workdir, SENTINEL_NAME)
        recorded = None
        if os.path.exists(sentinel):
            with open(sentinel, encoding="utf-8") as handle:
                recorded = handle.read().strip()
        return proc, recorded, workdir

    def test_success_records_zero(self):
        proc, recorded, _ = self.run_script("true")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(recorded, "0")

    def test_failure_records_the_exit_code(self):
        proc, recorded, _ = self.run_script("exit 42")
        self.assertEqual(proc.returncode, 42)
        self.assertEqual(recorded, "42")

    def test_a_failing_command_that_does_not_call_exit(self):
        _proc, recorded, _ = self.run_script("false")
        self.assertEqual(recorded, "1")

    def test_a_missing_binary_is_recorded_not_lost(self):
        _proc, recorded, _ = self.run_script("definitely_not_a_real_command_xyz")
        self.assertEqual(recorded, "127")

    def test_the_payload_actually_runs(self):
        _proc, _recorded, workdir = self.run_script("echo hello > {stem}.out")
        with open(os.path.join(workdir, "mol.out"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "hello")

    def test_a_stale_sentinel_is_cleared_before_the_run(self):
        workdir = tempfile.mkdtemp(prefix="stale_")
        with open(os.path.join(workdir, SENTINEL_NAME), "w", encoding="utf-8") as handle:
            handle.write("99")
        script = get_scheduler("shell").build_script(
            "t", SubmitPreset(command_template="true"), "mol.inp", "job.log"
        )
        path = os.path.join(workdir, "run.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        subprocess.run([BASH, path], capture_output=True, timeout=60, cwd=workdir)
        with open(os.path.join(workdir, SENTINEL_NAME), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "0")

    def test_scheduler_directives_are_inert_comments(self):
        # A SLURM script must still be a runnable bash script.
        _proc, recorded, _ = self.run_script("true", scheduler_name="slurm")
        self.assertEqual(recorded, "0")

    def test_pbs_and_sge_scripts_also_run(self):
        for name in ("pbs", "sge"):
            _proc, recorded, _ = self.run_script("exit 3", scheduler_name=name)
            self.assertEqual(recorded, "3", name)

    def test_pre_commands_run_before_the_payload(self):
        workdir = tempfile.mkdtemp(prefix="pre_")
        preset = SubmitPreset(
            command_template="echo $MOLEDITPY_TEST > out.txt",
            pre_commands=["export MOLEDITPY_TEST=configured"],
        )
        script = get_scheduler("shell").build_script("t", preset, "mol.inp", "job.log")
        path = os.path.join(workdir, "run.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        subprocess.run([BASH, path], capture_output=True, timeout=60, cwd=workdir)
        with open(os.path.join(workdir, "out.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "configured")


class TestAKilledJobIsNotRecordedAsSuccess(unittest.TestCase):
    """A scheduler kill must never leave a sentinel reading 0.

    Walltime, preemption, scancel and node drain all arrive as a signal. The
    EXIT trap alone sees $? == 0 in that case, so the job was reported DONE,
    rc=0 with truncated or absent output.
    """

    def _write_script(self, payload="sleep 30"):
        workdir = tempfile.mkdtemp(prefix="killed_job_")
        script = get_scheduler("shell").build_script(
            "t", SubmitPreset(command_template=payload), "mol.inp", "job.log"
        )
        path = os.path.join(workdir, "run.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        return workdir, path

    def _sentinel(self, workdir, timeout=8.0):
        target = os.path.join(workdir, SENTINEL_NAME)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(target) and os.path.getsize(target):
                with open(target, encoding="utf-8") as handle:
                    return handle.read().strip()
            time.sleep(0.1)
        return ""

    @unittest.skipUnless(BASH, "no bash available")
    def test_sigterm_is_not_recorded_as_success(self):
        # The payload signals the wrapper itself rather than relying on
        # Popen.terminate(), which on Windows is a hard TerminateProcess that
        # no shell trap can observe -- there the test would pass vacuously.
        workdir, path = self._write_script(payload="kill -TERM $$; sleep 30")
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        recorded = self._sentinel(workdir)
        # 143 means the trap ran; empty means the wrapper died first, which the
        # poller reports as LOST. Never 0.
        self.assertNotEqual(recorded, "0", "a killed job was recorded as a clean success")
        if recorded:
            self.assertEqual(recorded, "143")

    @unittest.skipUnless(BASH, "no bash available")
    def test_a_normal_exit_still_records_its_own_code(self):
        workdir, path = self._write_script(payload="exit 7")
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        self.assertEqual(self._sentinel(workdir), "7")

    @unittest.skipUnless(BASH, "no bash available")
    def test_success_still_records_zero(self):
        workdir, path = self._write_script(payload="true")
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        self.assertEqual(self._sentinel(workdir), "0")


if __name__ == "__main__":
    unittest.main()
