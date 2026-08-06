"""The no-SSH backend: the same workflow, run on this machine.

Everything above the transport is unchanged, so these check the two things the
backend actually promises -- commands run in a POSIX shell, and "upload" and
"download" are file copies -- plus the full submit/poll/fetch cycle against a
real bash, which is the only way to know the pieces fit.
"""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from job_manager import runner
from job_manager.models import (
    BACKEND_LOCAL,
    SCHEDULER_SHELL,
    HostProfile,
    Job,
    SubmitPreset,
)
from job_manager.transport import create_transport
from job_manager.transport.base import TransportError
from job_manager.transport.local import LocalTransport, find_shell, shell_available

BASH = shutil.which("bash") or find_shell()


def local_host(**kwargs):
    kwargs.setdefault("name", "this machine")
    kwargs.setdefault("backend", BACKEND_LOCAL)
    kwargs.setdefault("scheduler", SCHEDULER_SHELL)
    return HostProfile(**kwargs)


class TestTheFactoryPicksIt(unittest.TestCase):
    def test_a_local_host_gets_the_local_transport(self):
        self.assertIsInstance(create_transport(local_host()), LocalTransport)

    def test_it_needs_no_password(self):
        transport = create_transport(local_host(), password="ignored")
        self.assertIsInstance(transport, LocalTransport)

    def test_a_missing_shell_is_reported_not_crashed(self):
        transport = LocalTransport(local_host(), shell="")
        with patch("job_manager.transport.local.find_shell", return_value=""):
            transport._shell = ""
            with self.assertRaises(TransportError) as caught:
                transport.run("echo hi")
        self.assertIn("POSIX shell", str(caught.exception))

    def test_close_is_harmless(self):
        create_transport(local_host()).close()


@unittest.skipUnless(BASH, "no bash available")
class TestRunningCommands(unittest.TestCase):
    def setUp(self):
        self.transport = LocalTransport(local_host())

    def test_stdout_and_exit_code(self):
        result = self.transport.run("echo hello")
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertEqual(result.rc, 0)
        self.assertTrue(result.ok)

    def test_a_failing_command_reports_its_code(self):
        self.assertEqual(self.transport.run("exit 3").rc, 3)

    def test_stderr_is_captured_separately(self):
        result = self.transport.run("echo oops >&2")
        self.assertIn("oops", result.stderr)

    def test_login_commands_are_prepended(self):
        transport = LocalTransport(local_host(login_commands=["export MOLEDITPY_X=set"]))
        self.assertEqual(transport.run("echo $MOLEDITPY_X").stdout.strip(), "set")

    def test_a_timeout_is_reported(self):
        with self.assertRaises(TransportError) as caught:
            self.transport.run("sleep 5", timeout=1)
        self.assertIn("timed out", str(caught.exception))

    def test_test_connection_names_the_machine(self):
        self.assertTrue(self.transport.test_connection())


@unittest.skipUnless(BASH, "no bash available")
class TestMovingFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="local_files_")
        self.transport = LocalTransport(local_host())
        self.source = os.path.join(self.tmp, "mol.inp")
        with open(self.source, "w", encoding="utf-8") as handle:
            handle.write("input\n")

    def test_upload_is_a_copy(self):
        target = os.path.join(self.tmp, "jobs", "run1", "mol.inp")
        self.transport.upload(self.source, target)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "input\n")

    def test_upload_creates_missing_directories(self):
        target = os.path.join(self.tmp, "a", "b", "c", "mol.inp")
        self.transport.upload(self.source, target)
        self.assertTrue(os.path.exists(target))

    def test_download_is_a_copy_the_other_way(self):
        target = os.path.join(self.tmp, "results", "mol.out")
        self.transport.download(self.source, target)
        self.assertTrue(os.path.exists(target))

    def test_a_tilde_path_is_expanded(self):
        with patch("os.path.expanduser", lambda p: p.replace("~", self.tmp, 1)):
            self.transport.upload(self.source, "~/expanded/mol.inp")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "expanded", "mol.inp")))

    def test_copying_a_file_onto_itself_does_not_destroy_it(self):
        # The job directory and the download directory can be the same place.
        self.transport.download(self.source, self.source)
        with open(self.source, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "input\n")

    def test_a_missing_source_is_reported(self):
        with self.assertRaises(TransportError):
            self.transport.upload(os.path.join(self.tmp, "nope.inp"), "x/mol.inp")


@unittest.skipUnless(BASH, "no bash available")
class TestTheWholeCycleOnThisMachine(unittest.TestCase):
    """Submit, poll and fetch for real, with no network involved."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="local_cycle_").replace(os.sep, "/")
        self.host = local_host(remote_root=self.root)
        self.transport = create_transport(self.host)
        self.inputs = []
        source_dir = tempfile.mkdtemp(prefix="local_inputs_")
        for name in ("mol.inp", "basis.dat"):
            path = os.path.join(source_dir, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(name)
            self.inputs.append(path)

    def submit(self, command, name="job"):
        preset = SubmitPreset(command_template=command, fetch_globs=["*.txt", "*.out"])
        # JobService is what normally copies the preset's patterns onto the job;
        # these tests drive the runner directly, so do it here.
        job = Job(
            name=name,
            host_id=self.host.id,
            scheduler=SCHEDULER_SHELL,
            fetch_globs=list(preset.fetch_globs),
        )
        return runner.submit_job(self.transport, self.host, preset, job, self.inputs)

    def wait_for(self, job, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline:
            for job_id, state in runner.poll_host(self.transport, self.host, [job]).items():
                if job_id == job.id:
                    job.touch(state)
            if job.is_terminal:
                return job.state
            time.sleep(0.5)
        return job.state

    def test_a_job_runs_and_reports_done(self):
        job = self.submit("echo produced > result.txt")
        self.assertTrue(job.remote_job_id.isdigit(), job.remote_job_id)
        self.assertEqual(self.wait_for(job), "DONE")
        self.assertEqual(job.rc, 0)

    def test_a_failing_job_reports_its_exit_code(self):
        job = self.submit("exit 5")
        self.assertEqual(self.wait_for(job), "FAILED")
        self.assertEqual(job.rc, 5)

    def test_every_input_file_lands_in_the_job_directory(self):
        job = self.submit("ls > listing.txt")
        self.wait_for(job)
        listing = os.listdir(os.path.join(self.root, os.path.basename(job.remote_dir)))
        for name in ("mol.inp", "basis.dat", "moleditpy_run.sh"):
            self.assertIn(name, listing)

    def test_results_are_fetched_back(self):
        job = self.submit("echo produced > result.txt")
        self.wait_for(job)
        local_dir = tempfile.mkdtemp(prefix="local_results_")
        fetched = runner.fetch_results(self.transport, job, local_dir)
        self.assertIn("result.txt", [os.path.basename(p) for p in fetched])

    def test_the_log_can_be_tailed(self):
        job = self.submit("echo hello from the job")
        self.wait_for(job)
        self.assertIn("hello from the job", runner.tail_log(self.transport, job))


class TestShellDetection(unittest.TestCase):
    def test_it_reports_what_it_found(self):
        self.assertEqual(shell_available(), bool(find_shell()))

    def test_path_is_preferred(self):
        with patch("shutil.which", return_value="/usr/bin/bash"):
            self.assertEqual(find_shell(), "/usr/bin/bash")

    def test_nothing_found_is_empty_not_an_error(self):
        with patch("shutil.which", return_value=None), patch("os.path.isfile", return_value=False):
            self.assertEqual(find_shell(), "")


if __name__ == "__main__":
    unittest.main()
