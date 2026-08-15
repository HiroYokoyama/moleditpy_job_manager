"""Submit through the plugin, and check the job really runs.

Everything else about the runner is tested from one side or the other: the
runner script is driven as a live process, and the plugin's command order is
asserted against a fake transport. Neither noticed when the two stopped
agreeing.

Making the runner script content-addressed meant it was uploaded as
``moleditpy_runner_<digest>.sh`` while ``ensure_runner_command`` still defaulted
to the fixed old name. Every unit test passed. On a real host the plugin
uploaded one file, started another that did not exist, reported "started", and
left every job sitting at PENDING for ever -- with the reason only in a log file
nobody reads::

    bash: moleditpy_runner.sh: No such file or directory

So this drives the whole path -- submit, start, dispatch, run, finish -- with a
real shell and no fakes at all.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from job_manager import remote_runner
from job_manager.models import (
    MODE_RUNNER,
    SCHEDULER_SHELL,
    SCHEDULER_WINDOWS,
    HostProfile,
    Job,
)
from job_manager.runner import poll_runner, submit_to_runner
from job_manager.transport.local import LocalTransport

from .fakes import make_preset

from .bash_support import find_bash

BASH = find_bash()
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
ON_WINDOWS = os.name == "nt" and POWERSHELL is not None


class EndToEndCase(unittest.TestCase):
    """One host, one real shell, no fake transport."""

    scheduler = SCHEDULER_SHELL

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="e2e_")
        self.addCleanup(self._cleanup)
        self.input = os.path.join(self.root, "mol.inp")
        with open(self.input, "w", encoding="utf-8") as handle:
            handle.write("! opt\n")
        self.host = HostProfile(
            id="h",
            backend="local",
            scheduler=self.scheduler,
            concurrency_mode=MODE_RUNNER,
            remote_root=os.path.join(self.root, "jobs").replace("\\", "/"),
        )
        self.directory = remote_runner.runner_dir(self.host.remote_root)

    def _cleanup(self):
        # The runner exits when its queue empties; give it a moment, then take
        # the directory away regardless.
        time.sleep(0.2)
        shutil.rmtree(self.root, ignore_errors=True)

    def marker(self, name: str = "IT_RAN") -> str:
        return os.path.join(self.root, name).replace("\\", "/")

    def command_that_touches(self, path: str) -> str:
        return f"touch {path}"

    def submit(self, name="opt", command=None) -> Job:
        job = Job(name=name, host_id="h", scheduler=self.scheduler)
        preset = make_preset(command_template=command or self.command_that_touches(self.marker()))
        return submit_to_runner(LocalTransport(self.host), self.host, preset, job, [self.input])

    def wait_for(self, predicate, timeout=60.0, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        queue = os.listdir(os.path.join(self.directory, "queue"))
        log = os.path.join(self.directory, remote_runner.RUNNER_LOG_NAME)
        detail = ""
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="replace") as handle:
                detail = handle.read().strip()[:300]
        self.fail(f"timed out waiting for {what}; queue={queue}; runner.log={detail!r}")

    def listed(self, directory: str):
        return os.listdir(os.path.join(self.directory, directory))


@unittest.skipUnless(BASH, "needs a bash")
class TestBashEndToEnd(EndToEndCase):
    def test_a_submitted_job_actually_runs(self):
        # The whole point: submitting must start something that runs the job.
        self.submit()

        self.wait_for(lambda: os.path.exists(self.marker()), what="the job to run")

    def test_the_runner_script_the_plugin_starts_is_the_one_it_uploaded(self):
        # The regression exactly: uploaded one name, started another.
        self.submit()

        uploaded = [f for f in os.listdir(self.directory) if f.startswith("moleditpy_runner")]
        self.assertEqual(len(uploaded), 1, uploaded)
        self.wait_for(lambda: os.path.exists(self.marker()), what="the job to run")

    def test_nothing_is_left_in_the_queue(self):
        self.submit()

        self.wait_for(lambda: not self.listed("queue"), what="the queue to drain")
        self.wait_for(lambda: self.listed("done"), what="the entry to reach done/")

    def test_the_runner_log_records_no_failure_to_start(self):
        # Where the regression's only symptom appeared.
        self.submit()
        self.wait_for(lambda: os.path.exists(self.marker()), what="the job to run")

        log = os.path.join(self.directory, remote_runner.RUNNER_LOG_NAME)
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="replace") as handle:
                self.assertNotIn("No such file", handle.read())

    def test_the_exit_code_comes_back_through_a_poll(self):
        job = self.submit()
        self.wait_for(lambda: os.path.exists(self.marker()), what="the job to run")

        self.wait_for(
            lambda: poll_runner(LocalTransport(self.host), self.host, [job]).get(job.id)
            in ("DONE", "FAILED"),
            what="the poll to see it finish",
        )
        self.assertEqual(poll_runner(LocalTransport(self.host), self.host, [job])[job.id], "DONE")

    def test_a_second_job_runs_too(self):
        # The runner may have exited after the first; the next submission has
        # to start it again.
        self.submit(name="first")
        self.wait_for(lambda: os.path.exists(self.marker()), what="the first job")

        self.submit(name="second", command=self.command_that_touches(self.marker("SECOND")))

        self.wait_for(lambda: os.path.exists(self.marker("SECOND")), what="the second job")

    def test_a_missing_runner_script_is_reported_rather_than_silently_stuck(self):
        # nohup reports success for a file that is not there, so the failure
        # only ever reached a log file. Now it reaches the user.
        command = remote_runner.flavour_for(self.host).ensure_runner_command(
            self.directory, "moleditpy_runner_deadbeef.sh"
        )
        transport = LocalTransport(self.host)
        transport.run(remote_runner.prepare_command(self.directory))

        self.assertIn("missing", transport.run(command).stdout)


@unittest.skipUnless(ON_WINDOWS, "the PowerShell runner needs Windows")
class TestPowerShellEndToEnd(TestBashEndToEnd):
    """The same claims, in the other language."""

    scheduler = SCHEDULER_WINDOWS

    def command_that_touches(self, path: str) -> str:
        return f"New-Item -ItemType File -Path '{path}' | Out-Null"

    def test_a_missing_runner_script_is_reported_rather_than_silently_stuck(self):
        command = remote_runner.flavour_for(self.host).ensure_runner_command(
            self.directory, "moleditpy_runner_deadbeef.ps1"
        )
        transport = LocalTransport(self.host)
        transport.run(remote_runner.flavour_for(self.host).prepare_command(self.directory))

        self.assertIn("missing", transport.run(command).stdout)


if __name__ == "__main__":
    unittest.main()
