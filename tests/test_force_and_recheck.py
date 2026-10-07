"""Force run and LOST re-check, at the level of the commands sent to the host.

What the commands do on a real shell is in test_force_end_to_end; this is the
order they are sent in and how their answers are read -- which is where the
safety lives for forcing, and where the evidence is weighed for re-checking.
"""

import re
import unittest

from job_manager import dialect, remote_runner, remote_runner_ps
from job_manager.models import (
    MODE_RUNNER,
    SCHEDULER_SHELL,
    SCHEDULER_WINDOWS,
    SENTINEL_NAME,
    STATE_DONE,
    STATE_FAILED,
    STATE_LOST,
    STATE_PENDING,
    STATE_RUNNING,
    HostProfile,
    Job,
)
from job_manager.runner import (
    force_in_runner,
    poll_runner,
    recheck_job,
    stat_host_path,
    submit_to_runner,
)
from job_manager.transport.base import CommandResult, TransportError

from .fakes import FakeTransport, make_host, make_preset

MARK = "@@MOLEDITPY@@"


class FileTransport(FakeTransport):
    """Answers the plugin's file reads from a dict, the way a host would."""

    def __init__(self, host, files=None):
        super().__init__(host)
        self.files = dict(files or {})

    def run(self, cmd, timeout=None):
        if MARK in cmd and "cat " in cmd:
            self.commands.append(cmd)
            out = []
            for path in re.findall(r"cat (\S+) 2>/dev/null", cmd):
                out.append(MARK)
                out.append(self.files.get(path, dialect.MISSING))
            return CommandResult(0, "\n".join(out) + "\n", "")
        return super().run(cmd, timeout)


def runner_host(**kwargs):
    defaults = dict(
        id="box",
        name="box",
        hostname="box",
        scheduler=SCHEDULER_SHELL,
        concurrency_mode=MODE_RUNNER,
        remote_root="~/moleditpy_jobs",
        load_profile=False,
    )
    defaults.update(kwargs)
    return HostProfile(**defaults)


def runner_job(host, sequence=3, state=STATE_PENDING, **kwargs):
    job = Job(
        id="abc123",
        name="check",
        host_id=host.id,
        scheduler=host.scheduler,
        remote_dir="~/moleditpy_jobs/run1",
        log_file="job.log",
        state=state,
        **kwargs,
    )
    job.remote_job_id = remote_runner.entry_name(sequence, job.id)
    return job


class TestTheForceCommand(unittest.TestCase):
    def test_the_pid_file_is_created_before_the_entry_is_claimed(self):
        # A reap between the claim and the pid would retire the job unstarted.
        command = remote_runner.force_command("~/q", "job_0003_abc123.sh")
        self.assertLess(command.index('> "pids/job_0003_abc123.sh"'), command.index('mv "queue/'))
        self.assertIn("set -C", command)

    def test_the_placeholder_is_never_written_over_a_real_pid(self):
        command = remote_runner_ps.force_command("C:\\q", "job_0003_abc123.ps1")
        create = command.index("New-Item -ItemType File -Path")
        self.assertNotIn("-Force", command[create : command.index("Out-Null", create)])
        self.assertLess(create, command.index("Move-Item -LiteralPath 'queue"))

    def test_nothing_started_on_windows_inherits_the_commands_output(self):
        # Start-Process with -Redirect* hands the child every inheritable
        # handle, the plugin's own output pipe included: the command then
        # returned only when the child exited -- the helper, after the whole
        # queue; the forced job, after itself.
        for command in (
            remote_runner_ps.ensure_runner_command("C:\\q", "moleditpy_runner_v1.ps1"),
            remote_runner_ps.force_command("C:\\q", "job_0003_abc123.ps1"),
        ):
            with self.subTest(command=command[:40]):
                self.assertNotIn("-RedirectStandard", command)

    def test_the_windows_helper_keeps_its_own_log(self):
        script = remote_runner_ps.build_runner_script("C:\\q")
        self.assertIn("Start-Transcript", script)
        self.assertIn(remote_runner.RUNNER_LOG_NAME, script)

    def test_an_entry_the_plugin_did_not_write_is_refused(self):
        for module in (remote_runner, remote_runner_ps):
            with self.subTest(flavour=module.__name__), self.assertRaises(ValueError):
                module.force_command("~/q", "job_1_x; rm -rf ~.sh")

    def test_both_flavours_have_the_new_commands(self):
        for module in (remote_runner, remote_runner_ps):
            with self.subTest(flavour=module.__name__):
                self.assertTrue(callable(module.force_command))
                self.assertTrue(callable(module.queue_detail_command))


class TestForcing(unittest.TestCase):
    def setUp(self):
        self.host = runner_host()
        self.transport = FakeTransport(self.host)

    def test_a_forced_submission_is_started_after_it_is_queued_and_before_the_helper(self):
        job = Job(name="quick", host_id=self.host.id, scheduler=SCHEDULER_SHELL)
        submit_to_runner(self.transport, self.host, make_preset(), job, [], force=True)

        commands = self.transport.commands
        enqueue = next(i for i, c in enumerate(commands) if 'mv "tmp/' in c and '"queue/' in c)
        force = next(i for i, c in enumerate(commands) if "set -C" in c)
        helper = next(i for i, c in enumerate(commands) if "mkdir lock" in c)
        self.assertLess(enqueue, force)
        self.assertLess(force, helper)
        self.assertTrue(job.force_run)

    def test_an_ordinary_submission_forces_nothing(self):
        job = Job(name="normal", host_id=self.host.id, scheduler=SCHEDULER_SHELL)
        submit_to_runner(self.transport, self.host, make_preset(), job, [])
        self.assertFalse(self.transport.ran("set -C"))
        self.assertFalse(job.force_run)

    def test_forcing_a_queued_job_starts_a_helper_to_reap_it(self):
        self.transport.when("set -C", stdout="forced\n")
        self.assertTrue(force_in_runner(self.transport, self.host, runner_job(self.host)))
        self.assertTrue(self.transport.ran("mkdir lock"))

    def test_a_job_no_longer_waiting_is_not_an_error(self):
        self.transport.when("set -C", stdout="notqueued\n")
        self.assertFalse(force_in_runner(self.transport, self.host, runner_job(self.host)))
        self.assertFalse(self.transport.ran("mkdir lock"))

    def test_anything_else_is_reported(self):
        self.transport.when("set -C", stdout="", rc=1, stderr="Permission denied")
        with self.assertRaises(TransportError) as caught:
            force_in_runner(self.transport, self.host, runner_job(self.host))
        self.assertIn("Permission denied", str(caught.exception))

    def test_a_windows_host_gets_the_powershell_command(self):
        host = runner_host(scheduler=SCHEDULER_WINDOWS)
        transport = FakeTransport(host).when("New-Item -ItemType File -Path", stdout="forced\n")
        job = runner_job(host)
        job.remote_job_id = remote_runner.entry_name(3, job.id, ".ps1")
        self.assertTrue(force_in_runner(transport, host, job))
        self.assertFalse(transport.ran("set -C"))


class TestTheQueueDetail(unittest.TestCase):
    def test_waiting_jobs_come_back_in_dispatch_order_not_text_order(self):
        stdout = "\n".join(
            [
                "paused 1",
                "limit slots 2",
                "limit cores 8",
                "entry running job_0002_aaa.sh 6 0",
                "entry queue job_10000_ccc.sh 1 0",
                "entry queue job_9999_bbb.sh 4 2048",
                "entry queue not-an-entry 1 0",
            ]
        )
        detail = remote_runner.parse_queue_detail(stdout)
        self.assertTrue(detail["paused"])
        self.assertEqual(detail["limits"], {"slots": 2, "cores": 8})
        self.assertEqual([w["job_id"] for w in detail["waiting"]], ["bbb", "ccc"])
        self.assertEqual(detail["waiting"][0]["memory_mb"], 2048)
        self.assertEqual(detail["running"][0]["cores"], 6)

    def test_nothing_on_the_host_is_an_empty_queue(self):
        detail = remote_runner.parse_queue_detail("")
        self.assertEqual(detail, {"paused": False, "limits": {}, "running": [], "waiting": []})


class TestPollingFallsBackToTheHelpersRecord(unittest.TestCase):
    def setUp(self):
        self.host = runner_host()
        self.job = runner_job(self.host, state=STATE_RUNNING)
        status = f"~/moleditpy_jobs/.moleditpy_runner/status/{self.job.remote_job_id}"
        self.status_path = status
        self.sentinel = f"~/moleditpy_jobs/run1/{SENTINEL_NAME}"

    def poll(self, files):
        transport = FileTransport(self.host, files)
        transport.when("for d in queue running done", stdout=f"done {self.job.remote_job_id}\n")
        return poll_runner(transport, self.host, [self.job])

    def test_a_missing_exit_code_file_with_a_helper_record_is_not_lost(self):
        self.assertEqual(self.poll({self.status_path: "0"}), {self.job.id: STATE_DONE})
        self.assertEqual(self.job.rc, 0)

    def test_the_helper_record_carries_a_failure_too(self):
        self.assertEqual(self.poll({self.status_path: "3"}), {self.job.id: STATE_FAILED})

    def test_the_jobs_own_file_wins(self):
        updates = self.poll({self.sentinel: "0", self.status_path: "7"})
        self.assertEqual(updates, {self.job.id: STATE_DONE})

    def test_nothing_at_all_is_still_lost(self):
        self.assertEqual(self.poll({}), {self.job.id: STATE_LOST})

    def test_a_blocked_job_is_still_failed_with_its_reason(self):
        updates = self.poll({self.status_path: remote_runner.STATUS_BLOCKED})
        self.assertEqual(updates, {self.job.id: STATE_FAILED})
        self.assertIn("never started", self.job.last_error)


class TestRechecking(unittest.TestCase):
    def test_a_cluster_job_whose_exit_code_has_appeared_is_done(self):
        host = make_host()
        job = Job(
            id="j1",
            name="lostjob",
            host_id=host.id,
            remote_dir="~/runs/r1",
            remote_job_id="4242",
            state=STATE_LOST,
        )
        transport = FileTransport(host, {f"~/runs/r1/{SENTINEL_NAME}": "0"})
        transport.when("ls -p", stdout="mol.out\njob.log\n")

        report = recheck_job(transport, host, job)

        self.assertEqual(report["state"], STATE_DONE)
        self.assertEqual(report["rc"], 0)
        self.assertEqual(report["sentinel"], "0")
        self.assertEqual(report["files"], ["mol.out", "job.log"])
        # A copy was examined; the caller decides what to change.
        self.assertEqual(job.state, STATE_LOST)

    def test_a_job_the_queue_still_lists_is_running_again(self):
        host = make_host()
        job = Job(
            id="j1", host_id=host.id, remote_dir="~/r", remote_job_id="4242", state=STATE_LOST
        )
        transport = FileTransport(host).when("squeue", stdout="4242 RUNNING\n")
        self.assertEqual(recheck_job(transport, host, job)["state"], STATE_RUNNING)

    def test_with_no_evidence_it_stays_lost_and_says_what_it_saw(self):
        host = make_host()
        job = Job(
            id="j1", host_id=host.id, remote_dir="~/r", remote_job_id="4242", state=STATE_LOST
        )
        report = recheck_job(FileTransport(host), host, job)
        self.assertEqual(report["state"], STATE_LOST)
        self.assertEqual(report["sentinel"], dialect.MISSING)
        self.assertEqual(report["runner_status"], "")

    def test_a_helper_queue_job_is_read_from_the_helpers_record(self):
        host = runner_host()
        job = runner_job(host, state=STATE_LOST)
        status = f"~/moleditpy_jobs/.moleditpy_runner/status/{job.remote_job_id}"
        transport = FileTransport(host, {status: "0"})
        transport.when("for d in queue running done", stdout=f"done {job.remote_job_id}\n")
        report = recheck_job(transport, host, job)
        self.assertEqual(report["state"], STATE_DONE)
        self.assertEqual(report["runner_status"], "0")


class TestStat(unittest.TestCase):
    def test_a_file_reports_its_size_and_digest(self):
        host = make_host()
        digest = "a" * 64
        transport = FakeTransport(host).when("wc -c", stdout=f"FILE\n12\n{digest}\n")
        info = stat_host_path(transport, host, "/opt/a.out")
        self.assertEqual(
            info,
            {"path": "/opt/a.out", "exists": True, "type": "file", "size": 12, "sha256": digest},
        )

    def test_a_digest_tool_that_did_not_answer_leaves_it_empty(self):
        host = make_host()
        transport = FakeTransport(host).when("wc -c", stdout="FILE\n12\nsha256sum: not found\n")
        self.assertEqual(stat_host_path(transport, host, "/opt/a")["sha256"], "")

    def test_a_directory_and_nothing(self):
        host = make_host()
        transport = FakeTransport(host).when("wc -c", stdout="DIRECTORY\n")
        self.assertEqual(stat_host_path(transport, host, "/opt")["type"], "directory")
        transport = FakeTransport(host).when("wc -c", stdout="MISSING\n")
        self.assertFalse(stat_host_path(transport, host, "/nope")["exists"])


if __name__ == "__main__":
    unittest.main()
