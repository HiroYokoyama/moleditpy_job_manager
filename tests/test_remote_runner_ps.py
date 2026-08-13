"""The PowerShell runner, executed as a real process.

This is a second implementation of the queue, in a second language, and the
guarantees it has to keep are the same ones: run in order, never dispatch twice,
respect the limits, honour a dependency, hold on pause, and -- the delicate one
-- exit as soon as the queue empties without ever dropping a job enqueued in
that instant.

The bash suite proves those for bash. Nothing about that carries over, so they
are proved again here by running it.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from job_manager.remote_runner import (
    CORES_NAME,
    PAUSED_NAME,
    SLOTS_NAME,
    STATUS_BLOCKED,
    SUBDIRS,
    entry_name,
    next_sequence,
    parse_entry,
    parse_listing,
)
from job_manager.remote_runner_ps import (
    ENTRY_SUFFIX,
    RUNNER_SCRIPT_NAME,
    build_job_script,
    build_runner_script,
)

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

#: The runner is Windows-only by construction -- it builds `running\entry`
#: paths and cancels with taskkill, because it exists for the machine that has
#: no POSIX shell. GitHub's Ubuntu images ship pwsh, so "is there a PowerShell"
#: ran it on Linux, where a backslash is not a separator.
ON_WINDOWS = os.name == "nt" and POWERSHELL is not None

#: PowerShell starts processes far more slowly than bash, so "busy" has to
#: outlast a couple of launches for the intermediate states to be seen.
POLL = 0.2
BUSY = 2


class RunnerHarness(unittest.TestCase):
    """A runner directory on disk, driven exactly as the plugin drives it."""

    def setUp(self):
        if not ON_WINDOWS:
            self.skipTest("the PowerShell runner needs Windows, not just a PowerShell")
        self.tmp = tempfile.mkdtemp(prefix="ps_runner_")
        self.addCleanup(self._cleanup)
        self.dir = os.path.join(self.tmp, "runner")
        self.jobs = os.path.join(self.tmp, "jobs")
        for name in SUBDIRS:
            os.makedirs(os.path.join(self.dir, name), exist_ok=True)
        os.makedirs(self.jobs, exist_ok=True)
        self.script_path = os.path.join(self.dir, RUNNER_SCRIPT_NAME)
        self._write(self.script_path, build_runner_script(self.dir, poll_seconds=POLL))
        self.processes = []

    def _cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="ascii", newline="") as handle:
            handle.write(text)

    def set_limit(self, name: str, value) -> None:
        self._write(os.path.join(self.dir, name), f"{value}\r\n")

    def enqueue(
        self, job_id: str, body: str, cores: int = 1, after: str = "", require_success: bool = True
    ) -> str:
        """Write a job's wrapper and queue a script for it, as the plugin does."""
        job_dir = os.path.join(self.jobs, job_id)
        os.makedirs(job_dir, exist_ok=True)
        self._write(os.path.join(job_dir, "wrapper.ps1"), body + "\r\n")

        existing = []
        for name in ("queue", "running", "done"):
            existing += os.listdir(os.path.join(self.dir, name))
        entry = entry_name(next_sequence(existing), job_id, ENTRY_SUFFIX)

        self._write(
            os.path.join(self.dir, "tmp", entry),
            build_job_script(
                job_dir,
                "wrapper.ps1",
                "job.log",
                entry=entry,
                directory=self.dir,
                job_name=job_id,
                after_job_id=after,
                require_success=require_success,
                cores=cores,
            ),
        )
        # tmp then move, exactly as the real submission does: the runner must
        # never be able to start a half-written script.
        os.replace(os.path.join(self.dir, "tmp", entry), os.path.join(self.dir, "queue", entry))
        return entry

    def start_runner(self):
        process = subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        os.makedirs(os.path.join(self.dir, "lock"), exist_ok=True)
        return process

    def wait_for(self, predicate, timeout: float = 60.0, what: str = "condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}; queue={self.listing()}")

    def listing(self) -> dict:
        lines = []
        for name in ("queue", "running", "done"):
            for entry in sorted(os.listdir(os.path.join(self.dir, name))):
                lines.append(f"{name} {entry}")
        return parse_listing("\n".join(lines))

    def status(self, entry: str) -> str:
        path = os.path.join(self.dir, "status", entry)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return ""
        except PermissionError:
            # Windows shares files far less freely than POSIX: catching the
            # instant PowerShell has this open for writing is a sharing
            # violation, not a result. Poll again.
            return ""
        # Bytes on purpose: a BOM here is the bug being guarded against.
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "status file has a UTF-8 BOM")
        return raw.decode("ascii", "replace").strip()

    def marker(self, name: str) -> str:
        return os.path.join(self.tmp, name)


class TestItRunsWhatIsQueued(RunnerHarness):
    def test_a_queued_job_runs_and_records_its_exit_code(self):
        entry = self.enqueue("aaa", f"New-Item -ItemType File -Path '{self.marker('ran')}'")

        self.start_runner()

        self.wait_for(lambda: os.path.exists(self.marker("ran")), what="the job to run")
        self.wait_for(lambda: self.status(entry) == "0", what="the exit code")

    def test_a_failing_job_records_its_real_exit_code(self):
        entry = self.enqueue("bbb", "exit 3")

        self.start_runner()

        self.wait_for(lambda: self.status(entry) == "3", what="rc=3")

    def test_the_jobs_output_lands_in_its_own_log(self):
        # The queued script runs with the *runner's* working directory, so a
        # relative wrapper or log path would be resolved in the wrong place.
        entry = self.enqueue("ccc", "Write-Output 'hello from the job'")
        log = os.path.join(self.jobs, "ccc", "job.log")

        self.start_runner()

        self.wait_for(lambda: self.status(entry) == "0", what="the job to finish")
        self.wait_for(lambda: os.path.exists(log), what="the log to be written")
        with open(log, "rb") as handle:
            raw = handle.read()

        # Bytes, because the bug this guards was an encoding one: `>` in
        # Windows PowerShell 5.1 is Out-File, which writes UTF-16 with a BOM,
        # and the log came back in an encoding nothing downstream can read.
        self.assertFalse(raw.startswith(b"\xff\xfe"), "the log is UTF-16")
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "the log has a UTF-8 BOM")
        self.assertIn(b"hello from the job", raw)

    def test_jobs_run_in_queue_order(self):
        for name in ("aaa", "bbb", "ccc"):
            self.enqueue(name, f"Add-Content -Path '{self.marker('order')}' -Value '{name}'")
        self.set_limit(SLOTS_NAME, 1)

        self.start_runner()

        self.wait_for(
            lambda: len(self.listing()) == 3 and all(v == "done" for v in self.listing().values()),
            what="all three to finish",
        )
        with open(self.marker("order"), encoding="ascii") as handle:
            self.assertEqual(handle.read().split(), ["aaa", "bbb", "ccc"])

    def test_it_exits_once_the_queue_empties(self):
        self.enqueue("aaa", "exit 0")

        process = self.start_runner()

        self.wait_for(lambda: process.poll() is not None, what="the runner to exit")
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "lock")))

    def test_the_lock_holds_this_runners_own_pid_not_a_jobs(self):
        # $pid is an automatic variable in PowerShell; a runner that stored a
        # job's id in it would reap whichever process happened to match.
        self.enqueue("aaa", f"Start-Sleep -Seconds {BUSY}")
        process = self.start_runner()
        entry = entry_name(1, "aaa", ENTRY_SUFFIX)
        pid_file = os.path.join(self.dir, "pids", entry)
        # The pid is recorded just after the claim, so "running" appears first.
        self.wait_for(lambda: os.path.exists(pid_file), what="aaa's pid to be recorded")

        with open(pid_file, encoding="ascii") as handle:
            job_pid = handle.read().strip()

        self.assertTrue(job_pid.isdigit(), job_pid)
        self.assertNotEqual(job_pid, str(process.pid))


class TestTheLimits(RunnerHarness):
    def test_the_slot_limit_is_respected(self):
        for name in ("aaa", "bbb", "ccc"):
            self.enqueue(name, f"Start-Sleep -Seconds {BUSY}")
        self.set_limit(SLOTS_NAME, 2)

        self.start_runner()

        self.wait_for(
            lambda: sum(1 for v in self.listing().values() if v == "running") == 2,
            what="two jobs to be running",
        )
        time.sleep(BUSY / 4)
        self.assertLessEqual(sum(1 for v in self.listing().values() if v == "running"), 2)

    def test_cores_are_counted_not_just_jobs(self):
        self.enqueue("aaa", f"Start-Sleep -Seconds {BUSY}", cores=3)
        self.enqueue("bbb", f"Start-Sleep -Seconds {BUSY}", cores=3)
        self.set_limit(SLOTS_NAME, 8)
        self.set_limit(CORES_NAME, 4)

        self.start_runner()

        self.wait_for(lambda: self.listing().get("aaa") == "running", what="the first job")
        time.sleep(BUSY / 4)
        self.assertNotEqual(self.listing().get("bbb"), "running")

    def test_small_jobs_fit_alongside_each_other(self):
        self.enqueue("aaa", f"Start-Sleep -Seconds {BUSY}", cores=2)
        self.enqueue("bbb", f"Start-Sleep -Seconds {BUSY}", cores=2)
        self.set_limit(SLOTS_NAME, 8)
        self.set_limit(CORES_NAME, 4)

        self.start_runner()

        self.wait_for(
            lambda: sum(1 for v in self.listing().values() if v == "running") == 2,
            what="both jobs to run at once",
        )

    def test_a_job_larger_than_the_machine_still_runs(self):
        entry = self.enqueue("aaa", "exit 0", cores=99)
        self.set_limit(CORES_NAME, 4)

        self.start_runner()

        self.wait_for(lambda: self.status(entry) == "0", what="the oversized job")


class TestDependencies(RunnerHarness):
    def test_a_dependent_job_waits_for_its_predecessor(self):
        self.enqueue(
            "aaa",
            f"Start-Sleep -Seconds {BUSY / 2}; Add-Content -Path '{self.marker('order')}' -Value 'aaa'",
        )
        self.enqueue("bbb", f"Add-Content -Path '{self.marker('order')}' -Value 'bbb'", after="aaa")
        self.set_limit(SLOTS_NAME, 4)

        self.start_runner()

        self.wait_for(lambda: self.listing().get("bbb") == "done", what="the dependent job")
        with open(self.marker("order"), encoding="ascii") as handle:
            self.assertEqual(handle.read().split(), ["aaa", "bbb"])

    def test_a_failed_predecessor_blocks_the_job_behind_it(self):
        self.enqueue("aaa", "exit 1")
        entry = self.enqueue(
            "bbb", f"New-Item -ItemType File -Path '{self.marker('ran')}'", after="aaa"
        )

        self.start_runner()

        self.wait_for(lambda: self.status(entry) == STATUS_BLOCKED, what="bbb to be blocked")
        self.assertFalse(os.path.exists(self.marker("ran")))

    def test_a_failed_predecessor_releases_it_when_success_is_not_required(self):
        self.enqueue("aaa", "exit 1")
        self.enqueue(
            "bbb",
            f"New-Item -ItemType File -Path '{self.marker('ran')}'",
            after="aaa",
            require_success=False,
        )

        self.start_runner()

        self.wait_for(lambda: os.path.exists(self.marker("ran")), what="bbb to run")

    def test_waiting_on_a_job_that_was_never_queued_does_not_hang_the_runner(self):
        entry = self.enqueue("bbb", "exit 0", after="nosuchjob")

        process = self.start_runner()

        self.wait_for(lambda: self.status(entry) == STATUS_BLOCKED, what="bbb blocked")
        self.wait_for(lambda: process.poll() is not None, what="the runner to exit")


class TestPause(RunnerHarness):
    def test_pausing_holds_the_queue(self):
        open(os.path.join(self.dir, PAUSED_NAME), "w").close()
        self.enqueue("aaa", f"New-Item -ItemType File -Path '{self.marker('ran')}'")

        self.start_runner()

        time.sleep(BUSY / 2)
        self.assertFalse(os.path.exists(self.marker("ran")))
        self.assertEqual(self.listing().get("aaa"), "queue")

    def test_resuming_lets_it_move_again(self):
        paused = os.path.join(self.dir, PAUSED_NAME)
        open(paused, "w").close()
        self.enqueue("aaa", f"New-Item -ItemType File -Path '{self.marker('ran')}'")
        self.start_runner()
        time.sleep(BUSY / 2)

        os.remove(paused)

        self.wait_for(lambda: os.path.exists(self.marker("ran")), what="the job to run")

    def test_pausing_does_not_kill_a_running_job(self):
        self.enqueue(
            "aaa",
            f"Start-Sleep -Seconds {BUSY}; New-Item -ItemType File -Path '{self.marker('done')}'",
        )
        self.start_runner()
        self.wait_for(lambda: self.listing().get("aaa") == "running", what="aaa running")

        open(os.path.join(self.dir, PAUSED_NAME), "w").close()

        self.wait_for(lambda: os.path.exists(self.marker("done")), what="aaa to finish anyway")


class TestEntryNames(unittest.TestCase):
    """Shared with the bash flavour on purpose: the two must not disagree."""

    def test_a_powershell_entry_round_trips(self):
        entry = entry_name(7, "a1b2", ENTRY_SUFFIX)

        self.assertTrue(entry.endswith(".ps1"))
        self.assertEqual(parse_entry(entry), (7, "a1b2"))

    def test_both_flavours_sort_and_parse_the_same_way(self):
        self.assertEqual(
            parse_entry(entry_name(3, "abc", ".sh")),
            parse_entry(entry_name(3, "abc", ".ps1")),
        )

    def test_a_listing_of_powershell_entries_is_understood(self):
        stdout = f"running {entry_name(1, 'aaa', ENTRY_SUFFIX)}"

        self.assertEqual(parse_listing(stdout), {"aaa": "running"})


if __name__ == "__main__":
    unittest.main()
