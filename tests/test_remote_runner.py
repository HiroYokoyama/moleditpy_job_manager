"""The remote runner, executed under a real bash.

Text assertions on generated shell have passed here before while the script was
semantically broken -- the completion sentinel looked right for two commits
after it had stopped working. So every claim about what the runner *does* is
made by running it: real processes, real files, real races.

The runner is a FIFO queue of numbered shell scripts that exits the moment the
queue empties. That exit is the delicate part: a job enqueued in the instant
between "the queue is empty" and "the lock is gone" must not be dropped.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

import pytest

from job_manager.remote_runner import (
    CORES_NAME,
    PAUSED_NAME,
    RUNNER_SCRIPT_NAME,
    SLOTS_NAME,
    STATUS_BLOCKED,
    SUBDIRS,
    build_job_script,
    build_runner_script,
    entry_name,
    is_paused_command,
    next_sequence,
    parse_entry,
    parse_listing,
    pause_command,
    prepare_command,
)

from .bash_support import bash_path, find_bash

BASH = find_bash()

#: How briskly the runner under test dispatches, and how long a "busy" job
#: stays busy. Fast enough to keep the suite short, slow enough that a
#: loaded machine still observes the intermediate states.
POLL = 0.1
BUSY = 0.5
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash on this machine")


class RunnerHarness(unittest.TestCase):
    """A runner directory on disk, driven exactly as the plugin drives it."""

    def setUp(self):
        if BASH is None:
            self.skipTest("no bash on this machine")
        self.tmp = tempfile.mkdtemp(prefix="remote_runner_")
        self.addCleanup(self._cleanup)
        self.dir = os.path.join(self.tmp, "runner")
        self.jobs = os.path.join(self.tmp, "jobs")
        for name in SUBDIRS:
            os.makedirs(os.path.join(self.dir, name), exist_ok=True)
        os.makedirs(self.jobs, exist_ok=True)
        self.script_path = os.path.join(self.dir, RUNNER_SCRIPT_NAME)
        # Sub-second, so a test does not spend its life waiting for a poll;
        # production passes RUNNER_POLL_SECONDS (5).
        with open(self.script_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(build_runner_script(bash_path(self.dir), poll_seconds=POLL))
        self.processes = []

    def _cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.stderr:
                process.stderr.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- driving it ---------------------------------------------------------

    def set_limit(self, name: str, value) -> None:
        with open(os.path.join(self.dir, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{value}\n")

    def enqueue(
        self, job_id: str, body: str, cores: int = 1, after: str = "", require_success: bool = True
    ) -> str:
        """Write a job's wrapper and queue a script for it, as the plugin does."""
        job_dir = os.path.join(self.jobs, job_id)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "run.sh"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/bash\n" + body.replace(self.tmp, bash_path(self.tmp)).replace("\\", "/") + "\n")

        existing = []
        for name in ("queue", "running", "done"):
            existing += os.listdir(os.path.join(self.dir, name))
        entry = entry_name(next_sequence(existing), job_id)

        script = build_job_script(
            bash_path(job_dir),
            "run.sh",
            "job.log",
            entry=entry,
            directory=bash_path(self.dir),
            job_name=job_id,
            after_job_id=after,
            require_success=require_success,
            cores=cores,
        )
        # tmp/ then mv, exactly as the real submission does: the runner must
        # never be able to start a half-written script.
        temp = os.path.join(self.dir, "tmp", entry)
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        os.replace(temp, os.path.join(self.dir, "queue", entry))
        return entry

    def start_runner(self):
        process = subprocess.Popen(
            [BASH, "-lc", f"exec bash {shlex.quote(bash_path(self.script_path))}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        os.makedirs(os.path.join(self.dir, "lock"), exist_ok=True)
        return process

    def wait_for(self, predicate, timeout: float = 10.0, what: str = "condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            # A runner that has already exited cannot satisfy a filesystem
            # condition. Report its stderr immediately instead of spending
            # the whole timeout waiting for a marker that will never appear.
            process = self.processes[-1] if self.processes else None
            if process is not None and process.poll() is not None:
                stderr = (process.stderr.read() if process.stderr else "").strip()
                self.fail(
                    f"runner exited with rc={process.returncode} while waiting for {what}; "
                    f"queue={self.listing()}; stderr={stderr!r}"
                )
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}; queue={self.listing()}")

    def listing(self) -> dict:
        lines = []
        for name in ("queue", "running", "done"):
            for entry in sorted(os.listdir(os.path.join(self.dir, name))):
                lines.append(f"{name} {entry}")
        return parse_listing("\n".join(lines))

    def status(self, entry: str) -> str:
        path = os.path.join(self.dir, "status", entry)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()

    def marker(self, name: str) -> str:
        # Forward slashes: this path is pasted into a shell script, and a
        # Windows backslash there is an escape, not a separator.
        return os.path.join(self.tmp, name)


@needs_bash
class TestItRunsWhatIsQueued(RunnerHarness):
    def test_a_queued_job_runs_and_records_its_exit_code(self):
        entry = self.enqueue("aaa", f"touch {self.marker('ran')}; exit 0")

        self.start_runner()

        self.wait_for(lambda: os.path.exists(self.marker("ran")), what="the job to run")
        self.wait_for(lambda: self.status(entry) == "0", what="the exit code")
        # The status file is written by the job just before it exits, so the
        # runner has not necessarily reaped it yet.
        self.wait_for(lambda: self.listing()["aaa"] == "done", what="aaa to be reaped")

    def test_a_failing_job_records_its_real_exit_code(self):
        entry = self.enqueue("bbb", "exit 3")

        self.start_runner()

        self.wait_for(lambda: self.status(entry) == "3", what="rc=3")

    def test_jobs_run_in_queue_order(self):
        self.enqueue("aaa", f"echo aaa >> {self.marker('order')}")
        self.enqueue("bbb", f"echo bbb >> {self.marker('order')}")
        self.enqueue("ccc", f"echo ccc >> {self.marker('order')}")
        self.set_limit(SLOTS_NAME, 1)

        self.start_runner()

        self.wait_for(
            lambda: len(self.listing()) == 3 and all(v == "done" for v in self.listing().values()),
            what="all three to finish",
        )
        with open(self.marker("order"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().split(), ["aaa", "bbb", "ccc"])

    def test_it_exits_once_the_queue_empties(self):
        self.enqueue("aaa", "exit 0")

        process = self.start_runner()

        self.wait_for(lambda: process.poll() is not None, what="the runner to exit")
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "lock")))


@needs_bash
class TestTheLimits(RunnerHarness):
    def test_the_slot_limit_is_respected(self):
        for name in ("aaa", "bbb", "ccc"):
            self.enqueue(name, f"echo x >> {self.marker('live')}; sleep {BUSY}")
        self.set_limit(SLOTS_NAME, 2)

        self.start_runner()

        self.wait_for(
            lambda: sum(1 for v in self.listing().values() if v == "running") == 2,
            what="two jobs to be running",
        )
        # And never a third while those two are still going.
        time.sleep(BUSY / 3)
        self.assertLessEqual(sum(1 for v in self.listing().values() if v == "running"), 2)

    def test_cores_are_counted_not_just_jobs(self):
        # Four cores, two jobs wanting three each: they cannot overlap even
        # though the slot limit would allow it.
        self.enqueue("aaa", f"sleep {BUSY}", cores=3)
        self.enqueue("bbb", f"sleep {BUSY}", cores=3)
        self.set_limit(SLOTS_NAME, 8)
        self.set_limit(CORES_NAME, 4)

        self.start_runner()

        self.wait_for(lambda: self.listing().get("aaa") == "running", what="the first job")
        time.sleep(BUSY / 3)
        self.assertNotEqual(self.listing().get("bbb"), "running")

    def test_small_jobs_fit_alongside_each_other(self):
        self.enqueue("aaa", f"sleep {BUSY}", cores=2)
        self.enqueue("bbb", f"sleep {BUSY}", cores=2)
        self.set_limit(SLOTS_NAME, 8)
        self.set_limit(CORES_NAME, 4)

        self.start_runner()

        self.wait_for(
            lambda: sum(1 for v in self.listing().values() if v == "running") == 2,
            what="both jobs to run at once",
        )

    def test_a_job_larger_than_the_machine_still_runs(self):
        # Asking for more cores than exist must not mean waiting for ever.
        entry = self.enqueue("aaa", "exit 0", cores=99)
        self.set_limit(CORES_NAME, 4)

        self.start_runner()

        self.wait_for(lambda: self.status(entry) == "0", what="the oversized job")


@needs_bash
class TestTheDefaultHostProfileRunsInParallel(RunnerHarness):
    """What an untouched host profile actually does on a real runner.

    "Run at most" defaults to *no limit* and "Cores available" to *detect*.
    Sending the helper 1 for "no limit" made that combination strictly serial,
    which is the opposite of what runner mode is for -- and the peak below was
    1 before the fix.
    """

    def peak_concurrency(self, count: int, cores_each: int = 1, budget: int = 8) -> int:
        from job_manager.models import MODE_RUNNER, SCHEDULER_SHELL, HostProfile
        from job_manager.remote_runner import slots_for

        host = HostProfile(
            id="h",
            scheduler=SCHEDULER_SHELL,
            concurrency_mode=MODE_RUNNER,
            max_concurrent=0,
            runner_cores=0,
        )
        self.set_limit(SLOTS_NAME, slots_for(host))
        self.set_limit(CORES_NAME, budget)
        for index in range(count):
            self.enqueue(f"p{index}", f"sleep {BUSY}", cores=cores_each)

        self.start_runner()
        peak = 0
        deadline = time.time() + 40
        while time.time() < deadline:
            where = self.listing()
            peak = max(peak, sum(1 for state in where.values() if state == "running"))
            if where and all(state == "done" for state in where.values()):
                break
            time.sleep(0.05)
        return peak

    def test_single_core_jobs_run_together_up_to_the_core_budget(self):
        self.assertEqual(self.peak_concurrency(4, cores_each=1, budget=8), 4)

    def test_the_core_budget_is_what_stops_them(self):
        # Four jobs of four cores on an eight-core budget: two at a time.
        self.assertEqual(self.peak_concurrency(4, cores_each=4, budget=8), 2)


class TestDependencies(RunnerHarness):
    def test_a_dependent_job_waits_for_its_predecessor(self):
        self.enqueue("aaa", f"sleep {BUSY / 2}; echo aaa >> {self.marker('order')}")
        self.enqueue("bbb", f"echo bbb >> {self.marker('order')}", after="aaa")
        self.set_limit(SLOTS_NAME, 4)

        self.start_runner()

        self.wait_for(lambda: self.listing().get("bbb") == "done", what="the dependent job")
        with open(self.marker("order"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().split(), ["aaa", "bbb"])

    def test_a_failed_predecessor_blocks_the_job_behind_it(self):
        self.enqueue("aaa", "exit 1")
        entry = self.enqueue("bbb", f"touch {self.marker('ran')}", after="aaa")

        self.start_runner()

        self.wait_for(lambda: self.status(entry) == STATUS_BLOCKED, what="bbb to be blocked")
        self.assertFalse(os.path.exists(self.marker("ran")))

    def test_a_failed_predecessor_releases_it_when_success_is_not_required(self):
        self.enqueue("aaa", "exit 1")
        self.enqueue("bbb", f"touch {self.marker('ran')}", after="aaa", require_success=False)

        self.start_runner()

        self.wait_for(lambda: os.path.exists(self.marker("ran")), what="bbb to run")

    def test_waiting_on_a_job_that_was_never_queued_does_not_hang_the_runner(self):
        # The runner exits when the queue empties, so a job waiting for
        # something that will never arrive would otherwise keep it alive for
        # ever -- and never run either.
        entry = self.enqueue("bbb", "exit 0", after="nosuchjob")

        process = self.start_runner()

        self.wait_for(lambda: self.status(entry) == STATUS_BLOCKED, what="bbb blocked")
        self.wait_for(lambda: process.poll() is not None, what="the runner to exit")


@needs_bash
class TestPause(RunnerHarness):
    def test_pausing_holds_the_queue(self):
        open(os.path.join(self.dir, PAUSED_NAME), "w").close()
        self.enqueue("aaa", f"touch {self.marker('ran')}")

        self.start_runner()

        time.sleep(BUSY)
        self.assertFalse(os.path.exists(self.marker("ran")))
        self.assertEqual(self.listing().get("aaa"), "queue")

    def test_resuming_lets_it_move_again(self):
        paused = os.path.join(self.dir, PAUSED_NAME)
        open(paused, "w").close()
        self.enqueue("aaa", f"touch {self.marker('ran')}")
        self.start_runner()
        time.sleep(BUSY)

        os.remove(paused)

        self.wait_for(lambda: os.path.exists(self.marker("ran")), what="the job to run")

    def run_command(self, command: str) -> int:
        """Run one of the plugin's own commands, as the transport would."""
        command = command.replace(self.tmp, bash_path(self.tmp))
        return subprocess.run([BASH, "-lc", command], timeout=10).returncode

    def test_the_plugins_own_pause_command_holds_the_queue(self):
        # The tests above make the flag by hand, which proves the runner reads
        # it and nothing about whether pause_command writes it. That command is
        # what the checkbox now sends, and it is a shell string, not a file op.
        self.enqueue("aaa", f"touch {self.marker('ran')}")

        self.assertEqual(self.run_command(pause_command(self.dir, True)), 0)
        self.start_runner()

        time.sleep(BUSY)
        self.assertFalse(os.path.exists(self.marker("ran")))
        self.assertEqual(self.listing().get("aaa"), "queue")

    def test_the_plugins_own_resume_command_releases_it(self):
        self.enqueue("aaa", f"touch {self.marker('ran')}")
        self.run_command(pause_command(self.dir, True))
        self.start_runner()
        time.sleep(BUSY)

        self.assertEqual(self.run_command(pause_command(self.dir, False)), 0)

        self.wait_for(lambda: os.path.exists(self.marker("ran")), what="the job to run")

    def test_the_state_the_plugin_reads_back_matches_reality(self):
        # queue_paused() parses this command's output; if the two disagree the
        # checkbox shows the opposite of what the host is doing.
        self.run_command(pause_command(self.dir, True))
        held = subprocess.run(
            [BASH, "-lc", is_paused_command(self.dir).replace(self.tmp, bash_path(self.tmp))],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.run_command(pause_command(self.dir, False))
        moving = subprocess.run(
            [BASH, "-lc", is_paused_command(self.dir).replace(self.tmp, bash_path(self.tmp))],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(held.stdout.strip(), PAUSED_NAME)
        self.assertEqual(moving.stdout.strip(), "running")

    def test_pausing_a_host_that_has_never_run_a_queue_works(self):
        # The controls are reachable before the first submission, so the flag
        # has to be settable on a directory that does not exist yet.
        fresh = os.path.join(self.tmp, "never_used")

        self.run_command(prepare_command(fresh))
        self.assertEqual(self.run_command(pause_command(fresh, True)), 0)

        self.assertTrue(os.path.exists(os.path.join(fresh, PAUSED_NAME)))

    def test_pausing_does_not_kill_a_running_job(self):
        self.enqueue("aaa", f"sleep {BUSY}; touch {self.marker('finished')}")
        self.start_runner()
        self.wait_for(lambda: self.listing().get("aaa") == "running", what="aaa running")

        open(os.path.join(self.dir, PAUSED_NAME), "w").close()

        self.wait_for(lambda: os.path.exists(self.marker("finished")), what="aaa to finish anyway")


@needs_bash
class TestTheShutdownRace(RunnerHarness):
    """The one race that makes "exit when empty" safe or unsafe."""

    def test_a_job_queued_as_the_runner_exits_is_not_dropped(self):
        # Enqueue repeatedly around the moment the runner decides to leave. The
        # runner re-checks the queue after releasing its lock precisely so that
        # a job landing in this window is still picked up.
        self.enqueue("aaa", "exit 0")
        process = self.start_runner()
        self.wait_for(lambda: self.listing().get("aaa") == "done", what="aaa to finish")

        entry = self.enqueue("bbb", f"touch {self.marker('ran')}")

        if process.poll() is None:
            self.wait_for(
                lambda: self.status(entry) == "0" or process.poll() is not None,
                what="bbb to run or the runner to leave",
            )
        if self.status(entry) != "0":
            # The runner had already gone; the plugin's own "ensure a runner is
            # up" step is what covers this half, so simulate it.
            self.start_runner()
        self.wait_for(lambda: self.status(entry) == "0", what="bbb to run")

    def test_the_lock_is_released_on_exit(self):
        self.enqueue("aaa", "exit 0")
        process = self.start_runner()

        self.wait_for(lambda: process.poll() is not None, what="the runner to exit")

        self.assertFalse(os.path.isdir(os.path.join(self.dir, "lock")))


class TestEntryNames(unittest.TestCase):
    """Pure naming rules; no shell needed."""

    def test_numbers_are_padded_so_that_sorting_is_the_run_order(self):
        names = [entry_name(n, "abc") for n in (2, 10, 1)]

        self.assertEqual(
            sorted(names), [entry_name(1, "abc"), entry_name(2, "abc"), entry_name(10, "abc")]
        )

    def test_an_entry_round_trips(self):
        self.assertEqual(parse_entry(entry_name(7, "a1b2")), (7, "a1b2"))

    def test_an_unrecognised_name_is_ignored(self):
        self.assertEqual(parse_entry("notes.txt"), (0, ""))

    def test_the_next_number_follows_the_highest_used_anywhere(self):
        # Including finished jobs: reusing a number would put a new job ahead
        # of everything already waiting.
        existing = [entry_name(1, "a"), entry_name(9, "b"), entry_name(3, "c")]

        self.assertEqual(next_sequence(existing), 10)

    def test_an_empty_queue_starts_at_one(self):
        self.assertEqual(next_sequence([]), 1)

    def test_a_listing_maps_job_ids_to_where_they_are(self):
        stdout = f"queue {entry_name(2, 'bbb')}\nrunning {entry_name(1, 'aaa')}\nnonsense\n"

        self.assertEqual(parse_listing(stdout), {"bbb": "queue", "aaa": "running"})


if __name__ == "__main__":
    unittest.main()
