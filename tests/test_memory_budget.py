"""Memory as a second budget beside the cores.

Cores alone are not enough to decide what can run together: two jobs of 90 GB
on a 120 GB machine must not both start because the cores happened to be free.
Overcommitting cores makes a calculation slow; overcommitting memory gets it
killed hours in, which is the one failure the user cannot recover from.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from job_manager import remote_runner, remote_runner_ps
from job_manager.remote_runner import build_runner_script
from job_manager.models import MODE_RUNNER, SCHEDULER_SHELL, SCHEDULER_WINDOWS, SubmitPreset
from job_manager.schedulers import (
    get_scheduler,
    parse_memory_mb,
    requested_cores,
    requested_memory_mb,
)

from .fakes import make_host

BASH = shutil.which("bash")


class TestParsingASizeRequest(unittest.TestCase):
    def test_gigabytes(self):
        self.assertEqual(parse_memory_mb("8G"), 8192)

    def test_gigabytes_spelled_out(self):
        self.assertEqual(parse_memory_mb("8GB"), 8192)

    def test_megabytes(self):
        self.assertEqual(parse_memory_mb("512M"), 512)

    def test_a_bare_number_is_megabytes(self):
        # The unit every queue system defaults to.
        self.assertEqual(parse_memory_mb("4000"), 4000)

    def test_whitespace_and_case(self):
        self.assertEqual(parse_memory_mb("  8 gb "), 8192)

    def test_a_fraction(self):
        self.assertEqual(parse_memory_mb("1.5G"), 1536)

    def test_nonsense_is_no_request_rather_than_a_wrong_number(self):
        for text in ("", "lots", "8 quatloos", None, "-4G"):
            self.assertEqual(parse_memory_mb(text), 0, repr(text))

    def test_a_preset_with_no_memory_asks_for_none(self):
        self.assertEqual(requested_memory_mb(SubmitPreset(memory="")), 0)


class TestTheScriptHeaderRecordsTheRequest(unittest.TestCase):
    """No queue reads these, so the head of the script is the record."""

    def script(self, scheduler, **kwargs):
        preset = SubmitPreset(command_template="orca {input}", **kwargs)
        return get_scheduler(scheduler).build_script(
            "j", preset, "mol.inp", "job.log", remote_dir="/jobs/x"
        )

    def test_bash_records_the_cores(self):
        self.assertIn(f"{remote_runner.CORES_TAG} 8", self.script("shell", cpus_per_task=8))

    def test_bash_records_the_memory(self):
        self.assertIn(f"{remote_runner.MEMORY_TAG} 8192", self.script("shell", memory="8G"))

    def test_powershell_records_them_too(self):
        script = self.script("windows", cpus_per_task=4, memory="16G")
        self.assertIn(f"{remote_runner.CORES_TAG} 4", script)
        self.assertIn(f"{remote_runner.MEMORY_TAG} 16384", script)

    def test_the_header_is_at_the_head(self):
        script = self.script("shell", cpus_per_task=2, memory="4G")
        self.assertLess(script.index(remote_runner.MEMORY_TAG), script.index("orca mol.inp"))

    def test_a_job_asking_for_no_memory_says_nothing(self):
        # A blank Memory field has always meant "no request", and a job that
        # asks for nothing must wait for nothing.
        self.assertNotIn(remote_runner.MEMORY_TAG, self.script("shell"))

    def test_the_lines_are_comments_so_the_script_still_runs(self):
        for line in self.script("shell", cpus_per_task=2, memory="4G").splitlines():
            if remote_runner.CORES_TAG in line or remote_runner.MEMORY_TAG in line:
                self.assertTrue(line.lstrip().startswith("#"), line)


class TestTheQueueEntryCarriesIt(unittest.TestCase):
    def entry(self, flavour, **kwargs):
        return flavour.build_job_script(
            "/jobs/x", "run.sh", "job.log", entry="job_0001_a.sh", directory="/r", **kwargs
        )

    def test_bash(self):
        script = self.entry(remote_runner, cores=4, memory_mb=8192)
        self.assertIn(f"{remote_runner.CORES_TAG} 4", script)
        self.assertIn(f"{remote_runner.MEMORY_TAG} 8192", script)

    def test_powershell(self):
        script = self.entry(remote_runner_ps, cores=4, memory_mb=8192)
        self.assertIn(f"{remote_runner.MEMORY_TAG} 8192", script)

    def test_no_request_writes_no_tag(self):
        self.assertNotIn(remote_runner.MEMORY_TAG, self.entry(remote_runner, cores=1, memory_mb=0))


class TestTheBudgetIsSentToTheHost(unittest.TestCase):
    def test_a_budget_is_written(self):
        command = remote_runner.set_memory_command("/r", 120 * 1024)
        self.assertIn(str(120 * 1024), command)
        self.assertIn(remote_runner.MEMORY_NAME, command)

    def test_detect_removes_the_file_so_the_machine_decides(self):
        self.assertIn("rm -f", remote_runner.set_memory_command("/r", 0))

    def test_powershell_removes_it_too(self):
        self.assertIn("Remove-Item", remote_runner_ps.set_memory_command(r"C:\r", 0))

    def test_setup_sends_all_three_limits_in_one_call(self):
        command = remote_runner.setup_command("/r", 4, 16, 65536)
        self.assertIn(remote_runner.SLOTS_NAME, command)
        self.assertIn(remote_runner.CORES_NAME, command)
        self.assertIn("65536", command)

    def test_the_windows_flavour_matches(self):
        command = remote_runner_ps.setup_command(r"C:\r", 4, 16, 65536)
        self.assertIn("65536", command)
        self.assertNotIn("&&", command)


class TestSubmissionSendsTheRequest(unittest.TestCase):
    """End to end: the preset's Memory reaches the queued script."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memsubmit_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = os.path.join(self.tmp, "mol.inp")
        with open(self.input, "w") as handle:
            handle.write("! opt\n")

    def test_the_memory_request_reaches_the_queue_entry(self):
        from job_manager.runner import submit_to_runner

        from .fakes import FakeTransport, make_job, make_preset

        host = make_host(
            scheduler=SCHEDULER_SHELL,
            concurrency_mode=MODE_RUNNER,
            runner_memory_mb=120 * 1024,
        )
        transport = FakeTransport(host)
        preset = make_preset(memory="90G", cpus_per_task=4)

        submit_to_runner(
            transport,
            host,
            preset,
            make_job(id="j1", remote_dir="", remote_job_id=""),
            [self.input],
        )

        queued = [text for path, text in transport.uploaded_text.items() if "job_0001" in path]
        self.assertTrue(queued, transport.uploaded_text.keys())
        self.assertIn(f"{remote_runner.MEMORY_TAG} {90 * 1024}", queued[0])

    def test_the_hosts_budget_reaches_the_host(self):
        from job_manager.runner import apply_queue_limits

        from .fakes import FakeTransport

        host = make_host(
            scheduler=SCHEDULER_SHELL,
            concurrency_mode=MODE_RUNNER,
            runner_memory_mb=120 * 1024,
        )
        transport = FakeTransport(host)

        apply_queue_limits(transport, host)

        self.assertIn(str(120 * 1024), transport.commands[0])


@unittest.skipUnless(BASH, "needs a bash")
class TestTheRunnerReallyHoldsBackOnMemory(unittest.TestCase):
    """The user's own case, run: two 90 GB jobs on a 120 GB machine."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_runner_")
        self.addCleanup(self._cleanup)
        self.dir = os.path.join(self.tmp, "runner").replace("\\", "/")
        self.jobs = os.path.join(self.tmp, "jobs").replace("\\", "/")
        for name in remote_runner.SUBDIRS:
            os.makedirs(os.path.join(self.dir, name), exist_ok=True)
        os.makedirs(self.jobs, exist_ok=True)
        self.script = os.path.join(self.dir, remote_runner.RUNNER_SCRIPT_NAME)
        with open(self.script, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(remote_runner.build_runner_script(self.dir, poll_seconds=0.2))
        self.processes = []

    def _cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def limit(self, name, value):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{value}\n")

    def enqueue(self, job_id, seconds, cores=1, memory_mb=0):
        job_dir = os.path.join(self.jobs, job_id).replace("\\", "/")
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "run.sh"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"#!/bin/bash\nsleep {seconds}\n")
        existing = []
        for name in ("queue", "running", "done"):
            existing += os.listdir(os.path.join(self.dir, name))
        entry = remote_runner.entry_name(remote_runner.next_sequence(existing), job_id)
        script = remote_runner.build_job_script(
            job_dir,
            "run.sh",
            "job.log",
            entry=entry,
            directory=self.dir,
            job_name=job_id,
            cores=cores,
            memory_mb=memory_mb,
        )
        temp = os.path.join(self.dir, "tmp", entry)
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        os.replace(temp, os.path.join(self.dir, "queue", entry))

    def start(self):
        process = subprocess.Popen(
            [BASH, self.script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.processes.append(process)
        os.makedirs(os.path.join(self.dir, "lock"), exist_ok=True)
        return process

    def peak_running(self, timeout=40.0):
        peak = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            where = {}
            for name in ("queue", "running", "done"):
                for entry in os.listdir(os.path.join(self.dir, name)):
                    where[entry] = name
            peak = max(peak, sum(1 for state in where.values() if state == "running"))
            if where and all(state == "done" for state in where.values()):
                break
            time.sleep(0.05)
        return peak

    def test_it_detects_the_machines_own_budgets_when_none_is_set(self):
        # "detect" is the default for both, so this is the branch every fresh
        # host profile takes -- and reading it wrong means either a stalled
        # queue or no limit at all.
        body = build_runner_script(self.dir, poll_seconds=0.2).split("while :; do")[0]
        probe = os.path.join(self.tmp, "probe.sh").replace("\\", "/")
        with open(probe, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body + '\necho "cores=$(total_cores) mem=$(total_memory)"\n')

        result = subprocess.run([BASH, probe], capture_output=True, text=True, timeout=60)

        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(part.split("=") for part in result.stdout.split())
        self.assertGreaterEqual(int(values["cores"]), 1)
        # 0 is allowed -- it means "do not schedule on memory" where the
        # machine cannot be asked -- but never a negative or a non-number.
        self.assertGreaterEqual(int(values["mem"]), 0)

    def test_two_ninety_gigabyte_jobs_do_not_share_a_hundred_and_twenty(self):
        # Cores are deliberately abundant: memory alone must hold the second
        # job back, which is the whole point of the second budget.
        self.limit(remote_runner.SLOTS_NAME, remote_runner.UNLIMITED_SLOTS)
        self.limit(remote_runner.CORES_NAME, 64)
        self.limit(remote_runner.MEMORY_NAME, 120 * 1024)
        self.enqueue("a", 2, cores=1, memory_mb=90 * 1024)
        self.enqueue("b", 2, cores=1, memory_mb=90 * 1024)

        self.start()

        self.assertEqual(self.peak_running(), 1)

    def test_two_fifty_gigabyte_jobs_do_share_it(self):
        # The other direction, or the test above would pass with a runner that
        # simply never runs two things at once.
        self.limit(remote_runner.SLOTS_NAME, remote_runner.UNLIMITED_SLOTS)
        self.limit(remote_runner.CORES_NAME, 64)
        self.limit(remote_runner.MEMORY_NAME, 120 * 1024)
        self.enqueue("a", 2, cores=1, memory_mb=50 * 1024)
        self.enqueue("b", 2, cores=1, memory_mb=50 * 1024)

        self.start()

        self.assertEqual(self.peak_running(), 2)

    def test_a_job_larger_than_the_machine_still_runs_alone(self):
        # Clamped rather than left queued for ever.
        self.limit(remote_runner.SLOTS_NAME, remote_runner.UNLIMITED_SLOTS)
        self.limit(remote_runner.CORES_NAME, 64)
        self.limit(remote_runner.MEMORY_NAME, 8 * 1024)
        self.enqueue("huge", 1, cores=1, memory_mb=64 * 1024)

        self.start()

        self.assertEqual(self.peak_running(), 1)

    def test_jobs_with_no_request_are_not_held_back(self):
        self.limit(remote_runner.SLOTS_NAME, remote_runner.UNLIMITED_SLOTS)
        self.limit(remote_runner.CORES_NAME, 64)
        self.limit(remote_runner.MEMORY_NAME, 1024)
        self.enqueue("a", 2, cores=1, memory_mb=0)
        self.enqueue("b", 2, cores=1, memory_mb=0)

        self.start()

        self.assertEqual(self.peak_running(), 2)


class TestTheDefaultHostUsesTheHelper(unittest.TestCase):
    """Otherwise none of the above happens unless a dropdown is found."""

    def test_a_new_no_queue_host_schedules_with_the_helper(self):
        self.assertTrue(make_host(scheduler=SCHEDULER_SHELL).uses_remote_runner)

    def test_a_new_windows_host_does_too(self):
        self.assertTrue(make_host(scheduler=SCHEDULER_WINDOWS).uses_remote_runner)

    def test_a_cluster_is_left_to_its_own_scheduler(self):
        self.assertFalse(make_host(scheduler="slurm").uses_remote_runner)

    def test_requested_cores_defaults_to_one(self):
        self.assertEqual(requested_cores(SubmitPreset()), 1)


if __name__ == "__main__":
    unittest.main()


class TestDetectingWhatTheHostHas(unittest.TestCase):
    """The Detect button, and the runner's own fallback, must count cores."""

    def test_the_probe_reports_all_three(self):
        for flavour in (remote_runner, remote_runner_ps):
            with self.subTest(flavour=flavour.__name__):
                command = flavour.probe_command()
                self.assertIn("cores=", command)
                self.assertIn("threads=", command)
                self.assertIn("memory=", command)

    def test_the_answer_is_parsed(self):
        self.assertEqual(
            remote_runner.parse_probe("cores=8 threads=16 memory=64000"), (8, 64000, 16)
        )

    def test_junk_is_zero_rather_than_a_wrong_number(self):
        self.assertEqual(remote_runner.parse_probe("bash: lscpu: not found"), (0, 0, 0))

    def test_a_partial_answer_keeps_what_it_got(self):
        self.assertEqual(remote_runner.parse_probe("cores=4 memory="), (4, 0, 0))

    def test_the_windows_probe_avoids_the_5_1_parser_errors(self):
        self.assertNotIn("&&", remote_runner_ps.probe_command())

    @unittest.skipUnless(BASH, "needs a bash")
    def test_it_counts_cores_and_not_threads(self):
        # nproc counts hardware threads, so a hyperthreaded machine reported
        # twice its real capacity and two six-core jobs landed on six cores.
        result = subprocess.run(
            [BASH, "-c", remote_runner.probe_command()],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        cores, memory, threads = remote_runner.parse_probe(result.stdout)

        self.assertGreaterEqual(cores, 1)
        self.assertGreater(memory, 0)
        # Never more cores than threads; on this machine they may be equal.
        self.assertLessEqual(cores, threads)

    @unittest.skipUnless(BASH, "needs a bash")
    def test_the_runner_agrees_with_the_probe(self):
        # The button must offer the number the queue would pick for itself,
        # or the dialog and the host disagree about the machine.
        # A real directory: the runner cds into its own before anything else,
        # and exits 1 if it cannot -- which would make this compare 0 with 0.
        tmp = tempfile.mkdtemp(prefix="agree_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        directory = tmp.replace("\\", "/")
        body = build_runner_script(directory, poll_seconds=0.2).split("while :; do")[0]
        script = os.path.join(tmp, "probe.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body + '\necho "cores=$(total_cores)"\n')

        from_runner = subprocess.run([BASH, script], capture_output=True, text=True, timeout=60)
        from_probe = subprocess.run(
            [BASH, "-c", remote_runner.probe_command()],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(
            remote_runner.parse_probe(from_runner.stdout)[0],
            remote_runner.parse_probe(from_probe.stdout)[0],
        )
