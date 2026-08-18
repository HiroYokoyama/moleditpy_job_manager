"""Every way a job can be submitted, driven end to end.

There are six of them, and they had grown apart: the direct shell path, the
same one with the helper queue, the native Windows pair, a job that runs in a
directory the user staged, and a job with no input file at all. Each is a
different combination of scheduler, transport and dialect, and a change to any
one of those can break exactly one of these without touching the others.

The point is that a job really runs: a script is generated, uploaded, started,
polled through its own sentinel, and its output fetched back. Text assertions
have passed here before while the generated script was semantically broken.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from job_manager import remote_runner
from job_manager.models import (
    BACKEND_LOCAL,
    BACKEND_WSL,
    MODE_LANES,
    MODE_RUNNER,
    SCHEDULER_SHELL,
    SCHEDULER_WINDOWS,
    STATE_DONE,
    STATE_FAILED,
    HostProfile,
    Job,
    SubmitPreset,
)
from job_manager.runner import (
    fetch_results,
    poll_host,
    poll_runner,
    submit_job,
    submit_to_runner,
    tail_log,
)
from job_manager.transport.local import LocalTransport, find_shell
from job_manager.transport.wsl import WSLTransport

from .bash_support import BASH

ON_WINDOWS = os.name == "nt"
POWERSHELL = find_shell("powershell") if ON_WINDOWS else ""
STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wsl_stub.py")


def wait_until(predicate, timeout: float = 30.0, step: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class SubmissionCase(unittest.TestCase):
    """One temporary root per test, and the job list that goes with it."""

    scheduler = SCHEDULER_SHELL
    backend = BACKEND_LOCAL
    mode = MODE_LANES

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="submitpath_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.work = tempfile.mkdtemp(prefix="submitwork_")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.host = HostProfile(
            name="here",
            backend=self.backend,
            scheduler=self.scheduler,
            remote_root=self.remote_root(),
            concurrency_mode=self.mode,
            max_concurrent=1 if self.mode == MODE_RUNNER else 0,
            runner_cores=2,
            runner_memory_mb=1024,
            load_profile=False,
        )
        self.transport = self.make_transport()
        self.addCleanup(self.transport.close)

    def remote_root(self) -> str:
        return self.root

    def make_transport(self):
        return LocalTransport(self.host)

    # --- what each flavour of host runs --------------------------------------

    def command(self, text: str) -> str:
        """A command that writes ``text`` into out.txt, in this host's shell."""
        if self.scheduler == SCHEDULER_WINDOWS:
            return f"cmd /c echo {text} > out.txt"
        return f"echo {text} > out.txt"

    def failing_command(self) -> str:
        if self.scheduler == SCHEDULER_WINDOWS:
            return "cmd /c exit 3"
        return "exit 3"

    def job(self, name: str = "probe") -> Job:
        return Job(
            name=name,
            host_id=self.host.id,
            host_name=self.host.name,
            scheduler=self.host.scheduler,
        )

    def preset(self, command: str, **overrides) -> SubmitPreset:
        fields = dict(command_template=command, fetch_globs=["*.txt"], cpus_per_task=1)
        fields.update(overrides)
        return SubmitPreset(**fields)

    def submit(self, job: Job, preset: SubmitPreset, files=()) -> Job:
        if self.host.uses_remote_runner:
            return submit_to_runner(self.transport, self.host, preset, job, list(files))
        return submit_job(self.transport, self.host, preset, job, list(files))

    def poll(self, job: Job) -> dict:
        if self.host.uses_remote_runner:
            return poll_runner(self.transport, self.host, [job])
        return poll_host(self.transport, self.host, [job])

    def run_to_completion(self, job: Job, timeout: float = 40.0) -> str:
        def settled() -> bool:
            updates = self.poll(job)
            if job.id in updates:
                job.state = updates[job.id]
            return job.is_terminal

        self.assertTrue(
            wait_until(settled, timeout=timeout),
            f"{job.name} never finished; last state {job.state}, log:\n{self.log(job)}",
        )
        return job.state

    def log(self, job: Job) -> str:
        try:
            return tail_log(self.transport, job, 40)
        except Exception as exc:  # the log is a diagnostic, never the assertion
            return f"(no log: {exc})"

    # --- the cases every host has to pass ------------------------------------

    def test_a_job_with_an_input_file_runs_and_comes_back(self):
        local_input = os.path.join(self.work, "mol.inp")
        with open(local_input, "w", encoding="utf-8") as handle:
            handle.write("a molecule\n")

        job = self.job()
        self.submit(job, self.preset(self.command("hello")), files=[local_input])
        self.assertTrue(job.remote_job_id, "no job id came back from the submission")

        self.assertEqual(self.run_to_completion(job), STATE_DONE)
        self.assertEqual(job.rc, 0)

        into = os.path.join(self.work, "results")
        fetched = fetch_results(self.transport, job, into)
        self.assertTrue(
            any(os.path.basename(path) == "out.txt" for path in fetched),
            f"out.txt was not fetched; got {fetched}",
        )
        content = open(os.path.join(into, "out.txt"), encoding="utf-8").read()
        self.assertIn("hello", content)

    def test_the_input_file_arrives_beside_the_job(self):
        local_input = os.path.join(self.work, "mol.inp")
        with open(local_input, "w", encoding="utf-8") as handle:
            handle.write("a molecule\n")
        job = self.job()
        self.submit(job, self.preset(self.command("hi")), files=[local_input])
        self.run_to_completion(job)
        listing = self.transport.run(self.list_command(job.remote_dir)).stdout
        self.assertIn("mol.inp", listing)

    def test_a_failing_job_is_recorded_as_failed_with_its_code(self):
        job = self.job("failer")
        self.submit(job, self.preset(self.failing_command()))
        self.assertEqual(self.run_to_completion(job), STATE_FAILED)
        self.assertEqual(job.rc, 3)

    def test_a_job_with_no_input_at_all_still_runs(self):
        job = self.job("commandonly")
        self.submit(job, self.preset(self.command("bare")))
        self.assertEqual(self.run_to_completion(job), STATE_DONE)

    def test_a_directory_the_user_prepared_is_used_as_it_is(self):
        staged = os.path.join(self.root, "staged")
        os.makedirs(staged, exist_ok=True)
        with open(os.path.join(staged, "there.inp"), "w", encoding="utf-8") as handle:
            handle.write("staged\n")

        job = self.job("staged")
        job.remote_dir = self.staged_path(staged)
        job.remote_dir_provided = True
        job.remote_input = "there.inp"
        self.submit(job, self.preset(self.command("staged")))
        self.assertEqual(self.run_to_completion(job), STATE_DONE)
        # Everything the wrapper writes there is named per job, because the
        # directory is the user's and may hold other jobs.
        self.assertIn(job.id[:6], job.script_name + job.sentinel_name + job.log_file)

    def staged_path(self, path: str) -> str:
        return path

    def list_command(self, directory: str) -> str:
        from job_manager import dialect

        return dialect.for_host(self.host).list_dir(directory)


@unittest.skipUnless(BASH, "needs a bash")
class TestLocalBashDirect(SubmissionCase):
    """The default: a POSIX shell on this machine, one process per job."""


@unittest.skipUnless(BASH, "needs a bash")
class TestLocalBashQueue(SubmissionCase):
    """The same host with the helper queue in front of it."""

    mode = MODE_RUNNER

    def tearDown(self):
        # The runner exits by itself when its queue empties, but a test that
        # failed early can leave one behind holding the temp directory open.
        directory = remote_runner.runner_dir(self.host.remote_root)
        self.transport.run(f"rm -rf {directory}/lock")


@unittest.skipUnless(ON_WINDOWS and POWERSHELL, "the Windows wrapper needs Windows")
class TestLocalWindowsDirect(SubmissionCase):
    """Native Windows: PowerShell all the way down, no POSIX shell anywhere."""

    scheduler = SCHEDULER_WINDOWS


@unittest.skipUnless(ON_WINDOWS and POWERSHELL, "the Windows wrapper needs Windows")
class TestLocalWindowsQueue(SubmissionCase):
    """Native Windows with the PowerShell helper queue."""

    scheduler = SCHEDULER_WINDOWS
    mode = MODE_RUNNER


@unittest.skipUnless(ON_WINDOWS and BASH, "the WSL command line is Windows-only")
class TestWSLThroughAStub(SubmissionCase):
    """The WSL path, with a stand-in for wsl.exe. See tests/wsl_stub.py.

    Not a mock of the transport: the transport is the real one, and what is
    replaced is the machine at the far end. The argv it builds, the Windows
    path it quotes inside the command, and the copies that carry files across
    are all executed.
    """

    backend = BACKEND_WSL

    def setUp(self):
        os.environ["WSL_STUB_BASH"] = BASH
        self.addCleanup(os.environ.pop, "WSL_STUB_BASH", None)
        super().setUp()

    def remote_root(self) -> str:
        # A "Linux" path, which for the stub is this machine's own bash view of
        # the temporary directory.
        return self.to_posix(self.root)

    def make_transport(self):
        transport = WSLTransport(self.host, exe=sys.executable)
        # sys.executable is the exe, so the stub script has to be the first
        # argument: python <stub> -d distro --cd / -- bash -lc <command>.
        original = transport._argv

        def argv(command: str) -> list:
            return [transport._require(), STUB] + original(command)[1:]

        transport._argv = argv
        return transport

    def to_posix(self, path: str) -> str:
        result = subprocess.run(
            [BASH, "-c", f"cygpath -a -u '{path}'"], capture_output=True, text=True
        )
        return (result.stdout or "").strip() or path

    def staged_path(self, path: str) -> str:
        return self.to_posix(path)

    def test_a_windows_input_file_is_translated_and_copied(self):
        local_input = os.path.join(self.work, "with space", "mol.inp")
        os.makedirs(os.path.dirname(local_input), exist_ok=True)
        with open(local_input, "w", encoding="utf-8") as handle:
            handle.write("crossing over\n")

        job = self.job("translated")
        self.submit(job, self.preset("cat {input} > out.txt"), files=[local_input])
        self.assertEqual(self.run_to_completion(job), STATE_DONE)

        into = os.path.join(self.work, "back")
        fetched = fetch_results(self.transport, job, into)
        self.assertTrue(fetched)
        self.assertIn("crossing over", open(os.path.join(into, "out.txt"), encoding="utf-8").read())


# The base class is a fixture, not a test case of its own: it has no host that
# is guaranteed to exist. Its subclasses carry every assertion.
del SubmissionCase


if __name__ == "__main__":
    unittest.main()
