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
from unittest.mock import patch

import pytest

from job_manager.models import (
    SENTINEL_NAME,
    STARTED_NAME,
    HostProfile,
    Job,
    SubmitPreset,
)
from job_manager.schedulers import get_scheduler
from job_manager.schedulers import base as scheduler_base
from job_manager.schedulers.base import WAIT_POLL_SECONDS
from job_manager.store import JobStore

from job_manager.models import MODE_LANES
from .fakes import FakeTransport, make_host

from .bash_support import find_bash

BASH = find_bash()


class TestEverySchedulerCanRunOneJobAfterAnother(unittest.TestCase):
    """Same feature, different mechanism: a queue takes a directive, the
    no-queue mode has the wrapper wait for the process itself."""

    def script(self, name, after="12345"):
        return get_scheduler(name).build_script(
            "j", SubmitPreset(command_template="orca mol.inp"), "mol.inp", "job.log", after
        )

    def test_all_four_support_it(self):
        for name in ("slurm", "pbs", "sge", "shell"):
            self.assertTrue(get_scheduler(name).supports_chaining, name)

    def test_slurm_uses_a_dependency(self):
        self.assertIn("#SBATCH --dependency=afterok:12345", self.script("slurm"))

    def test_pbs_uses_depend(self):
        self.assertIn("#PBS -W depend=afterok:12345", self.script("pbs"))

    def test_sge_uses_hold_jid(self):
        self.assertIn("#$ -hold_jid 12345", self.script("sge"))

    def test_the_no_queue_mode_waits_on_the_process(self):
        self.assertIn("kill -0 12345", self.script("shell"))
        self.assertNotIn("hold_jid", self.script("shell"))

    def test_a_queue_is_never_asked_to_wait_twice(self):
        # Emitting the directive *and* the wrapper block put
        # `while kill -0 <queue job id>` into a queue script, where that number
        # is a pid on the compute node belonging to some unrelated process --
        # so the job span in `sleep` until either that process exited or the
        # walltime ran out.
        for name in ("slurm", "pbs", "sge"):
            script = self.script(name)
            self.assertNotIn("kill -0", script, name)
            self.assertNotIn(STARTED_NAME, script, name)

    def test_a_directive_must_precede_the_first_command_or_the_queue_ignores_it(self):
        # Schedulers stop reading directives at the first executable line, and
        # a silently ignored dependency means the jobs run in the wrong order.
        for name in ("slurm", "pbs", "sge"):
            lines = self.script(name).splitlines()
            directive = next(i for i, line in enumerate(lines) if "12345" in line)
            first_command = next(
                i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")
            )
            self.assertLess(directive, first_command, name)

    def test_extra_directives_are_also_in_the_directive_block(self):
        # A hand-written `#$ -hold_jid` still has to be honoured.
        preset = SubmitPreset(
            command_template="orca mol.inp", extra_directives=["#$ -hold_jid 999"]
        )
        lines = get_scheduler("sge").build_script("j", preset, "mol.inp", "job.log").splitlines()
        directive = lines.index("#$ -hold_jid 999")
        first_command = next(
            i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")
        )
        self.assertLess(directive, first_command)

    def test_an_unusable_job_id_is_left_out_rather_than_written_into_a_directive(self):
        for payload in ("; rm -rf ~", "$(id)", "12345 && curl evil", ""):
            for name in ("slurm", "pbs", "sge"):
                self.assertEqual(get_scheduler(name).dependency_directives(payload), [], payload)

    def test_the_id_shapes_a_queue_really_uses_are_accepted(self):
        for job_id in ("12345", "123_4", "123.head.cluster", "123[]"):
            self.assertTrue(get_scheduler("slurm").dependency_directives(job_id), job_id)

    def test_a_queue_job_carries_the_dependency_end_to_end(self):
        from job_manager import runner

        host = make_host(scheduler="slurm")
        transport = FakeTransport(host).when("sbatch", stdout="99\n")
        job = Job(name="j", scheduler="slurm")
        runner.submit_job(transport, host, SubmitPreset(), job, [__file__], run_after="4242")
        self.assertIn("#SBATCH --dependency=afterok:4242", job.command)


class TestScheduledStart(unittest.TestCase):
    """ "Not before this time", using the queue's own flag where there is one."""

    def setUp(self):
        self.target = time.time() + 3600
        self.stamp = time.localtime(int(self.target))

    def script(self, name):
        return get_scheduler(name).build_script(
            "j",
            SubmitPreset(command_template="orca mol.inp"),
            "mol.inp",
            "job.log",
            start_after=self.target,
        )

    def test_slurm_uses_begin(self):
        expected = time.strftime("#SBATCH --begin=%Y-%m-%dT%H:%M:%S", self.stamp)
        self.assertIn(expected, self.script("slurm"))

    def test_pbs_uses_its_own_timestamp_format(self):
        # -a takes [[[[CC]YY]MM]DD]hhmm[.SS], not ISO.
        expected = time.strftime("#PBS -a %Y%m%d%H%M.%S", self.stamp)
        self.assertIn(expected, self.script("pbs"))

    def test_sge_uses_its_own_timestamp_format(self):
        expected = time.strftime("#$ -a %Y%m%d%H%M.%S", self.stamp)
        self.assertIn(expected, self.script("sge"))

    def test_the_no_queue_mode_sleeps_until_the_moment(self):
        self.assertIn(f'while [ "$(date +%s)" -lt {int(self.target)} ]', self.script("shell"))

    def test_a_queue_that_takes_a_start_time_does_not_also_sleep(self):
        for name in ("slurm", "pbs", "sge"):
            self.assertNotIn("date +%s", self.script(name), name)

    def test_it_compares_epochs_so_timezones_do_not_matter(self):
        # The comment shows a local time for the reader; the line that actually
        # runs must compare epoch seconds, or a remote machine in another
        # timezone would start the job at the wrong moment.
        executable = [
            line
            for line in self.script("shell").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        waits = [line for line in executable if "date +%s" in line]
        self.assertEqual(len(waits), 1)
        self.assertIn(str(int(self.target)), waits[0])
        self.assertNotIn(time.strftime("%H:%M", self.stamp), waits[0])

    def test_no_time_means_no_waiting(self):
        for name in ("slurm", "pbs", "sge", "shell"):
            script = get_scheduler(name).build_script(
                "j", SubmitPreset(command_template="true"), "mol.inp", "job.log"
            )
            self.assertNotIn("--begin", script, name)
            self.assertNotIn("date +%s", script, name)

    def test_a_time_in_the_past_is_still_emitted_and_simply_passes(self):
        script = get_scheduler("shell").build_script(
            "j", SubmitPreset(command_template="true"), "mol.inp", "job.log", start_after=1000
        )
        self.assertIn('while [ "$(date +%s)" -lt 1000 ]', script)

    def test_scheduled_and_chained_together(self):
        script = get_scheduler("slurm").build_script(
            "j",
            SubmitPreset(command_template="true"),
            "mol.inp",
            "job.log",
            run_after="12345",
            start_after=self.target,
        )
        self.assertIn("--dependency=afterok:12345", script)
        self.assertIn("--begin=", script)

    def test_the_job_record_remembers_the_time(self):
        job = Job(name="later", start_after=self.target)
        self.assertEqual(Job.from_dict(job.to_dict()).start_after, self.target)

    def test_an_older_record_without_the_field_loads(self):
        self.assertEqual(Job.from_dict({"id": "j1"}).start_after, 0.0)


class TestTheWaitBlock(unittest.TestCase):
    def script(self, run_after=""):
        return get_scheduler("shell").build_script(
            "j", SubmitPreset(command_template="true"), "mol.inp", "job.log", run_after
        )

    def test_no_pid_means_no_waiting(self):
        self.assertNotIn("kill -0", self.script())

    def test_the_wait_loop_is_generated(self):
        script = self.script("4242")
        self.assertIn(f"while kill -0 4242 2>/dev/null; do sleep {WAIT_POLL_SECONDS}; done", script)

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
        """Measured against a bare login shell, not against the clock.

        `bash -lc` sources the login profile, and on a loaded CI runner that
        alone took nearly as long as the job -- which read as "submit blocked"
        and failed on one leg of the matrix while the same command returned in
        0.11s on the others. What matters is that submitting costs no more than
        starting the shell, however slow that happens to be today.
        """
        payload = 10
        workdir = tempfile.mkdtemp(prefix="nonblocking_")
        with open(os.path.join(workdir, "run.sh"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"#!/bin/bash\nsleep {payload}\n")

        baseline_start = time.time()
        subprocess.run([BASH, "-lc", "true"], capture_output=True, timeout=60)
        baseline = time.time() - baseline_start

        command = get_scheduler("shell").submit_command("run.sh", "job.log")
        start = time.time()
        proc = subprocess.run(
            [BASH, "-lc", f"cd {workdir.replace(os.sep, '/')} && {command}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        elapsed = time.time() - start

        self.assertLess(
            elapsed,
            baseline + payload / 2,
            f"submit took {elapsed:.1f}s against a {baseline:.1f}s shell start, "
            f"so it waited for the {payload}s job",
        )
        self.assertTrue(proc.stdout.strip().isdigit(), proc.stdout)


@unittest.skipUnless(BASH, "no bash available")
class TestAChainRunsInOrder(unittest.TestCase):
    """Executed for real: the second script must not start until the first ends."""

    #: Patched into the generated script: the production value is 5 s, and a
    #: test that proves ordering does not need to prove it slowly.
    WAIT = 1

    @classmethod
    def setUpClass(cls):
        # Run the chain once for the whole class. Three tests each launching a
        # real predecessor, sleeping, and waiting on it cost three times over
        # for one set of facts.
        cls.workdir = cls.run_chained()
        cls.addClassCleanup(shutil.rmtree, cls.workdir, ignore_errors=True)

    @classmethod
    def run_chained(cls):
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
        if not pid.isdigit():
            raise AssertionError(f"no pid from the launch: {started.stdout}{started.stderr}")

        with patch.object(scheduler_base, "WAIT_POLL_SECONDS", cls.WAIT):
            script = get_scheduler("shell").build_script(
                "second",
                SubmitPreset(command_template="date +%s > second_started"),
                "mol.inp",
                "job.log",
                run_after=pid,
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
        workdir = self.workdir
        self.assertGreaterEqual(
            self.read(workdir, "second_started"),
            self.read(workdir, "first_done"),
            "the chained job ran before its predecessor finished",
        )

    def test_the_chained_job_still_records_its_exit_code(self):
        workdir = self.workdir
        with open(os.path.join(workdir, SENTINEL_NAME), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "0")

    def test_the_started_marker_appears(self):
        workdir = self.workdir
        self.assertTrue(os.path.exists(os.path.join(workdir, STARTED_NAME)))

    def test_a_predecessor_that_is_already_gone_does_not_hold_it_up(self):
        workdir = tempfile.mkdtemp(prefix="chain_dead_")
        script = get_scheduler("shell").build_script(
            "j",
            SubmitPreset(command_template="date +%s > ran"),
            "mol.inp",
            "job.log",
            run_after="999999",  # no such process
        )
        path = os.path.join(workdir, "run.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        start = time.time()
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        self.assertLess(time.time() - start, WAIT_POLL_SECONDS + 2)
        self.assertTrue(os.path.exists(os.path.join(workdir, "ran")))


class TestTheChainSurvivesASlowSubmission(unittest.TestCase):
    """Submitting twice quickly queues both workers before the first job has a
    pid. Reading it at dispatch time chained the second job behind nothing, so
    both ran at once -- the one thing chaining exists to prevent."""

    def setUp(self):
        pytest.importorskip("PyQt6.QtCore", reason="PyQt6 is not installed")
        from job_manager.service import JobService

        self.tmp = tempfile.mkdtemp(prefix="chain_race_")
        self.store = JobStore(self.tmp)
        # Lanes on purpose: this class is about the wrapper waiting for a
        # pid, which is what chaining does when there is no helper queue.
        self.host = make_host(scheduler="shell", concurrency_mode=MODE_LANES)
        self.store.add_host(self.host)
        self.service = JobService(self.store)
        self.service.pool = _DeferredPool()
        self.service.transport_for = lambda host: FakeTransport(host).when("chmod", stdout="4242\n")
        self.addCleanup(self.service.shutdown)
        self.input = os.path.join(self.tmp, "mol.inp")
        with open(self.input, "w", encoding="utf-8") as handle:
            handle.write("x")

    def submit(self, name, after=None):
        return self.service.submit(
            self.host, SubmitPreset(command_template="true"), name, [self.input], after_job=after
        )

    def test_the_pid_is_read_when_the_worker_runs_not_when_it_is_queued(self):
        first = self.submit("first")
        second = self.submit("second", after=first)
        self.assertEqual(first.remote_job_id, "", "precondition: no pid yet")
        for task in list(self.service.pool.queued):
            task.run_sync()
        self.assertIn("kill -0 4242", second.command)

    def test_a_predecessor_that_never_starts_does_not_hold_the_job_forever(self):
        first = self.submit("first")
        second = self.submit("second", after=first)
        first.touch("FAILED")  # its submission failed; no pid is ever coming
        start = time.time()
        self.service.pool.queued[1].run_sync()
        self.assertLess(time.time() - start, 5)
        self.assertNotIn("kill -0", second.command or "")

    def test_no_predecessor_means_no_waiting(self):
        self.assertEqual(self.service._chain_pid(None), "")


class _DeferredPool:
    """Queues tasks instead of running them, like a pool with no free thread."""

    def __init__(self):
        self.queued = []

    def start(self, task):
        self.queued.append(task)

    def setMaxThreadCount(self, count):
        pass

    def clear(self):
        self.queued.clear()

    def waitForDone(self, msecs=0):
        return True


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
