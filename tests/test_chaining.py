"""Running jobs one after another on a machine that has no queue.

A real scheduler serialises work for you; the plain background-process mode does
not, so two submissions used to start at once and fight over the same cores.
A chained job's wrapper waits for its predecessor's process before running
anything -- on the machine itself, so the chain keeps moving with MoleditPy
closed.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from job_manager.models import (
    SENTINEL_NAME,
    STARTED_NAME,
    HostProfile,
    Job,
    SubmitPreset,
)
from job_manager.schedulers import get_scheduler
from job_manager.schedulers.base import CHAIN_POLL_SECONDS
from job_manager.store import JobStore

from .fakes import FakeTransport, make_host

BASH = shutil.which("bash")


class TestOnlyTheQueuelessSchedulerChains(unittest.TestCase):
    def test_the_plain_scheduler_supports_it(self):
        self.assertTrue(get_scheduler("shell").supports_chaining)

    def test_the_real_queues_do_not(self):
        for name in ("slurm", "pbs", "sge"):
            self.assertFalse(get_scheduler(name).supports_chaining, name)

    def test_a_queue_scheduler_ignores_a_pid_even_if_asked(self):
        from job_manager import runner

        host = make_host(scheduler="slurm")
        transport = FakeTransport(host).when("sbatch", stdout="99\n")
        job = Job(name="j", scheduler="slurm")
        runner.submit_job(transport, host, SubmitPreset(), job, [__file__], wait_for_pid="4242")
        self.assertNotIn("kill -0 4242", job.command)


class TestTheWaitBlock(unittest.TestCase):
    def script(self, wait_for_pid=""):
        return get_scheduler("shell").build_script(
            "j", SubmitPreset(command_template="true"), "mol.inp", "job.log", wait_for_pid
        )

    def test_no_pid_means_no_waiting(self):
        self.assertNotIn("kill -0", self.script())

    def test_the_wait_loop_is_generated(self):
        script = self.script("4242")
        self.assertIn(
            f"while kill -0 4242 2>/dev/null; do sleep {CHAIN_POLL_SECONDS}; done", script
        )

    def test_it_comes_before_the_payload_and_the_pre_commands(self):
        preset = SubmitPreset(command_template="orca mol.inp", pre_commands=["module load orca"])
        script = get_scheduler("shell").build_script("j", preset, "mol.inp", "job.log", "4242")
        self.assertLess(script.index("kill -0 4242"), script.index("module load orca"))
        self.assertLess(script.index("kill -0 4242"), script.index("orca mol.inp"))

    def test_it_comes_after_the_traps_so_a_waiting_job_still_reports(self):
        script = self.script("4242")
        self.assertLess(script.index("' EXIT"), script.index("kill -0 4242"))

    def test_a_started_marker_is_written_when_the_wait_ends(self):
        self.assertIn(f"touch {STARTED_NAME}", self.script("4242"))

    def test_a_nonsense_pid_is_refused_rather_than_interpolated(self):
        for payload in ("; rm -rf ~", "$(id)", "", "  ", "abc"):
            self.assertNotIn("kill -0", self.script(payload), repr(payload))


class TestChoosingWhatToChainBehind(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(tempfile.mkdtemp(prefix="chain_tail_"))

    def add(self, job_id, state, when, host_id="h1"):
        self.store.add_job(Job(id=job_id, host_id=host_id, state=state, submitted_at=when))

    def test_nothing_to_chain_behind_on_an_idle_host(self):
        self.assertIsNone(self.store.chain_tail("h1"))

    def test_the_newest_active_job_is_the_tail(self):
        self.add("old", "RUNNING", 100)
        self.add("new", "RUNNING", 200)
        self.assertEqual(self.store.chain_tail("h1").id, "new")

    def test_a_third_job_queues_behind_the_second_not_the_running_one(self):
        # This is what makes a queue rather than two jobs waiting on one.
        self.add("first", "RUNNING", 100)
        self.add("second", "RUNNING", 200)
        self.assertEqual(self.store.chain_tail("h1").id, "second")

    def test_finished_jobs_are_not_a_tail(self):
        self.add("done", "DONE", 300)
        self.assertIsNone(self.store.chain_tail("h1"))

    def test_another_host_is_not_a_tail(self):
        self.add("elsewhere", "RUNNING", 100, host_id="h2")
        self.assertIsNone(self.store.chain_tail("h1"))


class TestSubmittingDoesNotBlock(unittest.TestCase):
    """`A && nohup B & echo $!` backgrounds the whole list, and that subshell
    holds the caller's stdout until the job ends -- so submitting a two-hour
    calculation held an ssh connection and a worker thread for two hours."""

    def test_only_the_nohup_is_backgrounded(self):
        command = get_scheduler("shell").submit_command("run.sh", "job.log")
        self.assertIn("{ nohup bash run.sh > job.log 2>&1 < /dev/null & }", command)
        self.assertTrue(command.endswith("&& echo $!"))

    @unittest.skipUnless(BASH, "no bash available")
    def test_it_really_returns_before_the_job_finishes(self):
        workdir = tempfile.mkdtemp(prefix="nonblocking_")
        with open(os.path.join(workdir, "run.sh"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/bash\nsleep 4\n")
        command = get_scheduler("shell").submit_command("run.sh", "job.log")
        start = time.time()
        proc = subprocess.run(
            [BASH, "-lc", f"cd {workdir.replace(os.sep, '/')} && {command}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        elapsed = time.time() - start
        self.assertLess(elapsed, 3.0, f"submit blocked for {elapsed:.1f}s")
        self.assertTrue(proc.stdout.strip().isdigit(), proc.stdout)


@unittest.skipUnless(BASH, "no bash available")
class TestAChainRunsInOrder(unittest.TestCase):
    """Executed for real: the second script must not start until the first ends."""

    def run_chained(self):
        """Launch the predecessor exactly as the plugin does, then chain behind it.

        The pid has to come from `$!` in the same shell that later runs
        `kill -0`. Taking it from Popen instead mixes pid namespaces on Windows
        -- Git Bash pids are MSYS pids, not Windows ones -- and the wait loop
        exits immediately against a pid it cannot see.
        """
        workdir = tempfile.mkdtemp(prefix="chain_exec_")
        posix_dir = workdir.replace(os.sep, "/")
        with open(os.path.join(workdir, "first.sh"), "w", encoding="utf-8", newline="\n") as h:
            h.write('#!/bin/bash\ncd "$(dirname "$0")" || exit 1\nsleep 3\ndate +%s > first_done\n')

        launch = get_scheduler("shell").submit_command("first.sh", "first.log")
        started = subprocess.run(
            [BASH, "-lc", f"cd {posix_dir} && {launch}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        pid = started.stdout.strip().splitlines()[-1]
        self.assertTrue(pid.isdigit(), started.stdout + started.stderr)

        script = get_scheduler("shell").build_script(
            "second",
            SubmitPreset(command_template="date +%s > second_started"),
            "mol.inp",
            "job.log",
            wait_for_pid=pid,
        )
        second = os.path.join(workdir, "second.sh")
        with open(second, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        subprocess.run([BASH, second], capture_output=True, timeout=60)
        return workdir

    def read(self, workdir, name):
        path = os.path.join(workdir, name)
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())

    def test_the_second_job_starts_after_the_first_finishes(self):
        workdir = self.run_chained()
        self.assertGreaterEqual(
            self.read(workdir, "second_started"),
            self.read(workdir, "first_done"),
            "the chained job ran before its predecessor finished",
        )

    def test_the_chained_job_still_records_its_exit_code(self):
        workdir = self.run_chained()
        with open(os.path.join(workdir, SENTINEL_NAME), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "0")

    def test_the_started_marker_appears(self):
        workdir = self.run_chained()
        self.assertTrue(os.path.exists(os.path.join(workdir, STARTED_NAME)))

    def test_a_predecessor_that_is_already_gone_does_not_hold_it_up(self):
        workdir = tempfile.mkdtemp(prefix="chain_dead_")
        script = get_scheduler("shell").build_script(
            "j",
            SubmitPreset(command_template="date +%s > ran"),
            "mol.inp",
            "job.log",
            wait_for_pid="999999",  # no such process
        )
        path = os.path.join(workdir, "run.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        start = time.time()
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        self.assertLess(time.time() - start, CHAIN_POLL_SECONDS + 2)
        self.assertTrue(os.path.exists(os.path.join(workdir, "ran")))


class TestTheJobRecordRemembersTheChain(unittest.TestCase):
    def test_after_job_id_round_trips(self):
        job = Job(name="second", after_job_id="abc123")
        self.assertEqual(Job.from_dict(job.to_dict()).after_job_id, "abc123")

    def test_it_defaults_to_empty(self):
        self.assertEqual(Job().after_job_id, "")

    def test_an_older_record_without_the_field_still_loads(self):
        self.assertEqual(Job.from_dict({"id": "j1", "name": "old"}).after_job_id, "")


class TestHostProfileTargets(unittest.TestCase):
    def test_a_local_host_describes_itself(self):
        self.assertEqual(HostProfile(backend="local").target, "this machine")

    def test_a_remote_host_is_unchanged(self):
        host = HostProfile(hostname="hpc.example.org", username="alice")
        self.assertEqual(host.target, "alice@hpc.example.org")


if __name__ == "__main__":
    unittest.main()
