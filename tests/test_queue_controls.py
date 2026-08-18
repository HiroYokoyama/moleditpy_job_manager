"""Holding the host's queue, and sending it new limits.

Both were implemented, tested and then unreachable: nothing outside the two
runner modules ever called ``pause_command``, while the docs advertised pause
as a feature of the helper. These tests cover the path that now connects them,
and the round trips that submitting no longer spends.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from job_manager import PLUGIN_VERSION, remote_runner, remote_runner_ps
from job_manager.models import MODE_RUNNER, SCHEDULER_SHELL, SCHEDULER_WINDOWS
from job_manager.runner import apply_queue_limits, queue_paused, set_queue_paused, submit_to_runner
from job_manager.transport.base import TransportError

from .fakes import FakeTransport, make_host, make_job, make_preset

from .bash_support import find_bash

BASH = find_bash()


def runner_host(**overrides):
    defaults = dict(
        scheduler=SCHEDULER_SHELL,
        concurrency_mode=MODE_RUNNER,
        max_concurrent=2,
        runner_cores=8,
    )
    defaults.update(overrides)
    return make_host(**defaults)


class TestReadingTheFlag(unittest.TestCase):
    def setUp(self):
        self.host = runner_host()
        self.transport = FakeTransport(self.host)

    def test_a_held_queue_reads_as_held(self):
        self.transport.when("paused", stdout="paused\n")
        self.assertTrue(queue_paused(self.transport, self.host))

    def test_a_moving_queue_reads_as_moving(self):
        self.transport.when("paused", stdout="running\n")
        self.assertFalse(queue_paused(self.transport, self.host))

    def test_a_host_with_no_runner_yet_is_not_held(self):
        # The command prints nothing at all where the directory does not exist.
        # "There is no queue yet" is not "the queue is held", and reporting it
        # as held would leave the box ticked for a host that has never run.
        self.assertFalse(queue_paused(self.transport, self.host))

    def test_the_windows_flavour_is_asked_in_powershell(self):
        host = runner_host(scheduler=SCHEDULER_WINDOWS)
        transport = FakeTransport(host)
        queue_paused(transport, host)
        self.assertTrue(transport.ran("Test-Path"))
        self.assertFalse(transport.ran("[ -f"))


class TestSettingTheFlag(unittest.TestCase):
    def setUp(self):
        self.host = runner_host()
        self.transport = FakeTransport(self.host)

    def test_pausing_creates_the_flag(self):
        self.assertTrue(set_queue_paused(self.transport, self.host, True))
        self.assertTrue(self.transport.ran("touch"))
        self.assertTrue(self.transport.ran(remote_runner.PAUSED_NAME))

    def test_resuming_removes_it(self):
        self.assertFalse(set_queue_paused(self.transport, self.host, False))
        self.assertTrue(self.transport.ran("rm -f"))

    def test_the_directory_is_prepared_first(self):
        # Holding a queue before the host has ever run one has to work, or the
        # only way to pause would be to submit something first.
        set_queue_paused(self.transport, self.host, True)
        self.assertTrue(self.transport.ran("mkdir -p"))
        self.assertLess(
            next(i for i, c in enumerate(self.transport.commands) if "mkdir -p" in c),
            next(i for i, c in enumerate(self.transport.commands) if "touch" in c),
        )

    def test_a_refusal_is_reported_rather_than_swallowed(self):
        # The checkbox goes back to what the host still says, which it can only
        # do if the failure reaches it.
        self.transport.when(remote_runner.PAUSED_NAME, rc=1, stderr="read-only file system")
        with self.assertRaises(TransportError):
            set_queue_paused(self.transport, self.host, True)


class TestApplyingLimits(unittest.TestCase):
    def setUp(self):
        self.host = runner_host(max_concurrent=3, runner_cores=12)
        self.transport = FakeTransport(self.host)

    def test_both_limits_are_sent(self):
        apply_queue_limits(self.transport, self.host)
        self.assertTrue(self.transport.ran("3"))
        self.assertTrue(self.transport.ran("12"))

    def test_it_costs_one_round_trip(self):
        # Three separate ssh processes before; each is a full handshake on
        # Windows, where OpenSSH cannot multiplex.
        apply_queue_limits(self.transport, self.host)
        self.assertEqual(len(self.transport.commands), 1)

    def test_no_limit_does_not_quietly_mean_one_at_a_time(self):
        # The control says "no limit" and the helper needs a number. Sending 1
        # made an untouched host profile a strictly serial queue, with nothing
        # on screen to explain why nothing ran in parallel.
        host = runner_host(max_concurrent=0)
        transport = FakeTransport(host)

        apply_queue_limits(transport, host)

        self.assertNotIn(f"echo 1 > {remote_runner.SLOTS_NAME}", transport.commands[0])
        self.assertIn(
            f"echo {remote_runner.UNLIMITED_SLOTS} > {remote_runner.SLOTS_NAME}",
            transport.commands[0],
        )

    def test_a_real_limit_is_still_honoured(self):
        host = runner_host(max_concurrent=2)
        transport = FakeTransport(host)
        apply_queue_limits(transport, host)
        self.assertIn(f"echo 2 > {remote_runner.SLOTS_NAME}", transport.commands[0])


class TestSlotsFor(unittest.TestCase):
    """0 means "no limit" everywhere else in the plugin; it must here too."""

    def test_no_limit_becomes_an_unbinding_number(self):
        self.assertEqual(
            remote_runner.slots_for(runner_host(max_concurrent=0)),
            remote_runner.UNLIMITED_SLOTS,
        )

    def test_a_limit_is_passed_through(self):
        self.assertEqual(remote_runner.slots_for(runner_host(max_concurrent=3)), 3)

    def test_a_negative_limit_is_treated_as_none(self):
        self.assertEqual(
            remote_runner.slots_for(runner_host(max_concurrent=-1)),
            remote_runner.UNLIMITED_SLOTS,
        )

    def test_the_unlimited_value_cannot_bind_before_the_cores_do(self):
        # A machine with more cores than this would schedule on slots instead,
        # which is the bug this constant exists to avoid.
        self.assertGreater(remote_runner.UNLIMITED_SLOTS, 4096)


class TestTheSetupCommand(unittest.TestCase):
    """One call in place of prepare + slots + cores + a version read."""

    def test_the_bash_flavour_prints_the_stored_version_last(self):
        command = remote_runner.setup_command("/tmp/r", 2, 4)
        self.assertIn("mkdir -p", command)
        self.assertIn(remote_runner.SLOTS_NAME, command)
        self.assertIn(remote_runner.CORES_NAME, command)
        # Ends by reporting the runner already there, if its file still is.
        self.assertIn(remote_runner.VERSION_NAME, command)
        self.assertTrue(command.rstrip().endswith("fi"))

    def test_the_powershell_flavour_covers_the_same_ground(self):
        command = remote_runner_ps.setup_command(r"C:\r", 2, 4)
        self.assertIn("New-Item", command)
        self.assertIn(remote_runner.VERSION_NAME, command)
        # 5.1 has no pipeline chain operators at all.
        self.assertNotIn("&&", command)

    @unittest.skipUnless(BASH, "needs a bash")
    def test_the_bash_setup_really_prepares_a_directory(self):
        tmp = tempfile.mkdtemp(prefix="setup_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        directory = os.path.join(tmp, "runner").replace("\\", "/")

        result = subprocess.run(
            [BASH, "-c", remote_runner.setup_command(directory, 3, 9)],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for name in remote_runner.SUBDIRS:
            self.assertTrue(os.path.isdir(os.path.join(directory, name)), name)
        with open(os.path.join(directory, remote_runner.SLOTS_NAME)) as handle:
            self.assertEqual(handle.read().strip(), "3")
        with open(os.path.join(directory, remote_runner.CORES_NAME)) as handle:
            self.assertEqual(handle.read().strip(), "9")
        # Nothing stored yet, so nothing to skip an upload on.
        self.assertEqual(result.stdout.strip(), "")

    @unittest.skipUnless(BASH, "needs a bash")
    def test_a_stored_version_comes_back_out(self):
        tmp = tempfile.mkdtemp(prefix="setup_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        directory = os.path.join(tmp, "runner").replace("\\", "/")

        subprocess.run([BASH, "-c", remote_runner.setup_command(directory, 1, 0)], timeout=60)
        subprocess.run(
            [BASH, "-c", remote_runner.store_version_command(directory, "abc123")], timeout=60
        )
        # The version names a script that has to be there: reporting one
        # whose file was deleted would skip the upload and then start nothing.
        open(os.path.join(directory, remote_runner.runner_script_name("abc123")), "w").close()
        again = subprocess.run(
            [BASH, "-c", remote_runner.setup_command(directory, 1, 0)],
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(again.stdout.strip().splitlines()[-1], "abc123")


class TestSubmissionSkipsWhatTheHostHas(unittest.TestCase):
    """The runner script is the same bytes every time; it was an scp per job."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="submit_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = os.path.join(self.tmp, "mol.inp")
        with open(self.input, "w") as handle:
            handle.write("! opt\n")
        self.host = runner_host()

    def submit(self, transport):
        return submit_to_runner(
            transport,
            self.host,
            make_preset(),
            make_job(id="j1", remote_dir="", remote_job_id=""),
            [self.input],
        )

    def uploaded_runner(self, transport) -> bool:
        # Versioned: moleditpy_runner_v<version>.sh, never a fixed name, so a
        # new version cannot be written over the file a running runner is
        # part way through reading.
        return any("moleditpy_runner_" in remote for _, remote in transport.uploads)

    def test_a_host_that_has_never_seen_it_gets_it(self):
        transport = FakeTransport(self.host)
        self.submit(transport)
        self.assertTrue(self.uploaded_runner(transport))
        self.assertTrue(transport.ran(remote_runner.VERSION_NAME))

    def test_a_host_that_already_has_it_is_not_sent_it_again(self):
        transport = FakeTransport(self.host)
        # What the setup call reports back from the host: this plugin's own
        # version, which is what the file on the host is now named after.
        transport.when("mkdir -p", stdout=f"{PLUGIN_VERSION}\n")

        self.submit(transport)

        self.assertFalse(self.uploaded_runner(transport))

    def test_a_changed_runner_is_sent_even_though_one_is_there(self):
        # A different plugin version is on the host; reusing that script
        # because "a runner exists" is how a queue ends up on stale code.
        transport = FakeTransport(self.host)
        transport.when("mkdir -p", stdout="0.0.0-not-the-current-version\n")

        self.submit(transport)

        self.assertTrue(self.uploaded_runner(transport))


if __name__ == "__main__":
    unittest.main()


class TestNothingIsOverwrittenOrRemoved(unittest.TestCase):
    """Generated scripts are a record, and one of them is being executed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="keep_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = os.path.join(self.tmp, "mol.inp")
        with open(self.input, "w") as handle:
            handle.write("! opt\n")
        self.host = runner_host()

    def submit(self, transport, job_id, name="opt"):
        return submit_to_runner(
            transport,
            self.host,
            make_preset(),
            make_job(id=job_id, name=name, remote_dir="", remote_job_id=""),
            [self.input],
        )

    def test_the_runner_script_is_named_after_the_plugin_version(self):
        # Overwriting the file a runner is executing is the hazard: bash reads
        # a script by byte offset as it goes, so replacing the contents makes a
        # running runner resume in the middle of different text.
        name = remote_runner.runner_script_name("0.19.0")
        self.assertIn("0.19.0", name)
        self.assertNotEqual(name, remote_runner.RUNNER_SCRIPT_NAME)

    def test_two_versions_are_two_files(self):
        first = remote_runner.runner_script_name("0.18.0")
        second = remote_runner.runner_script_name("0.19.0")
        self.assertNotEqual(first, second)

    def test_the_powershell_flavour_versions_its_own_too(self):
        name = remote_runner_ps.runner_script_name("0.19.0")
        self.assertIn("0.19.0", name)
        self.assertTrue(name.endswith(".ps1"))

    def test_a_reported_version_whose_file_is_gone_is_not_believed(self):
        # Otherwise the upload is skipped and a runner is started that is not
        # there. The setup command checks the file, not just the record.
        command = remote_runner.setup_command("/r", 1, 0)
        self.assertIn("-f", command)
        self.assertIn("moleditpy_runner_", command)

    def test_two_jobs_submitted_in_the_same_second_do_not_share_a_directory(self):
        # They would have overwritten each other's wrapper and inputs -- and
        # shared one .moleditpy_rc, so whichever finished first decided what
        # both jobs were reported to have done.
        first = self.submit(FakeTransport(self.host), "aaaaaaaaaaaa")
        second = self.submit(FakeTransport(self.host), "bbbbbbbbbbbb")

        self.assertNotEqual(first.remote_dir, second.remote_dir)

    def test_the_directory_still_sorts_by_submission_time(self):
        # The timestamp stays in front, which is what makes the listing
        # readable by hand.
        job = self.submit(FakeTransport(self.host), "aaaaaaaaaaaa")
        leaf = job.remote_dir.rsplit("/", 1)[-1]
        self.assertRegex(leaf, r"^\d{8}_\d{6}_opt_aaaaaaaaaaaa$")

    def test_each_job_keeps_its_own_queue_entry(self):
        # The entry carries the job id, so one job never claims another's.
        first = self.submit(FakeTransport(self.host), "aaaaaaaaaaaa")
        second = self.submit(FakeTransport(self.host), "bbbbbbbbbbbb")
        self.assertNotEqual(first.remote_job_id, second.remote_job_id)

    def test_nothing_in_the_queue_directories_is_ever_deleted(self):
        # Entries move queue -> running -> done and stay there. Only the pid
        # file of a finished job is removed, which is not a record of anything.
        script = remote_runner.build_runner_script("/r")
        for directory in ("queue", "running", "done", "status"):
            self.assertNotIn(f'rm -rf "{directory}', script)
            self.assertNotIn(f"rm -f {directory}/", script)


class TestTheDispatchNumberOnlyClimbs(unittest.TestCase):
    """The number *is* the dispatch order, so it must never go backwards."""

    def setUp(self):
        self.host = runner_host()

    def test_the_number_is_claimed_on_the_host(self):
        # Not worked out from a listing: a listing forgets everything the user
        # has deleted, and the count then restarts.
        command = remote_runner.claim_sequence_command("/r")
        self.assertIn(remote_runner.SEQUENCE_NAME, command)

    def test_both_flavours_claim_it(self):
        for flavour in (remote_runner, remote_runner_ps):
            with self.subTest(flavour=flavour.__name__):
                self.assertIn(remote_runner.SEQUENCE_NAME, flavour.claim_sequence_command("/r"))

    def test_the_windows_claim_avoids_the_5_1_parser_errors(self):
        self.assertNotIn("&&", remote_runner_ps.claim_sequence_command(r"C:\r"))

    def test_the_answer_is_parsed(self):
        self.assertEqual(remote_runner.parse_sequence("7\n"), 7)

    def test_noise_before_the_number_is_ignored(self):
        self.assertEqual(remote_runner.parse_sequence("bash: warning\n42\n"), 42)

    def test_no_answer_is_zero_rather_than_a_guess(self):
        self.assertEqual(remote_runner.parse_sequence("cd: no such directory"), 0)

    def test_a_host_that_will_not_give_a_number_fails_the_submission(self):
        # Rather than queueing at a number that means nothing.
        tmp = tempfile.mkdtemp(prefix="seqfail_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "mol.inp")
        with open(path, "w") as handle:
            handle.write("! opt\n")
        transport = FakeTransport(self.host)
        transport.when(remote_runner.SEQUENCE_NAME, rc=1, stderr="read-only file system")

        with self.assertRaises(TransportError):
            submit_to_runner(
                transport,
                self.host,
                make_preset(),
                make_job(id="j1", remote_dir="", remote_job_id=""),
                [path],
            )

    def test_the_runner_dispatches_in_numeric_order_not_alphabetical(self):
        # Past 9999 the padding runs out, and `sort` puts job_10000 before
        # job_9999 -- the dispatch order inverted exactly when a queue has been
        # busy for a long time.
        self.assertIn("sort -t_ -k2,2n", remote_runner.build_runner_script("/r"))

    def test_the_windows_runner_sorts_numerically_too(self):
        script = remote_runner_ps.build_runner_script(r"C:\r")
        self.assertIn("[int](($_.Name -split '_')[1])", script)


@unittest.skipUnless(BASH, "needs a bash")
class TestClaimingRunForReal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claim_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = os.path.join(self.tmp, "runner").replace("\\", "/")
        self.sh(remote_runner.setup_command(self.dir, 1, 0))

    def sh(self, command):
        return subprocess.run([BASH, "-c", command], capture_output=True, text=True, timeout=60)

    def claim(self) -> int:
        return remote_runner.parse_sequence(
            self.sh(remote_runner.claim_sequence_command(self.dir)).stdout
        )

    def forget_the_counter(self) -> None:
        """As if the counter file had never been written, or was lost."""
        path = os.path.join(self.dir, remote_runner.SEQUENCE_NAME)
        if os.path.exists(path):
            os.remove(path)

    def finish(self, number: int) -> None:
        name = remote_runner.entry_name(number, "abcabcabcabc")
        open(os.path.join(self.dir, "done", name), "w").close()

    def test_successive_claims_climb(self):
        self.assertEqual([self.claim(), self.claim(), self.claim()], [1, 2, 3])

    def test_clearing_the_history_does_not_restart_the_count(self):
        # The user's disk, so clearing done/ is their right -- but a number
        # reissued puts a new job ahead of everything still waiting.
        for number in (self.claim(), self.claim()):
            self.finish(number)
        for name in os.listdir(os.path.join(self.dir, "done")):
            os.remove(os.path.join(self.dir, "done", name))

        self.assertEqual(self.claim(), 3)

    def test_a_number_already_in_the_queue_is_never_reissued(self):
        # The counter file could be lost too; the queue is the other source.
        self.forget_the_counter()
        self.finish(12)

        self.assertEqual(self.claim(), 13)

    def test_a_zero_padded_number_is_not_read_as_octal(self):
        # $((0008)) is an error in bash, not eight.
        self.forget_the_counter()
        self.finish(8)

        self.assertEqual(self.claim(), 9)


class TestResubmittingKeepsTheOldRun(unittest.TestCase):
    """A resubmit is a new job, and must not reuse anything of the old one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resub_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = os.path.join(self.tmp, "mol.inp")
        with open(self.input, "w") as handle:
            handle.write("! opt\n")
        self.host = runner_host()

    def submit(self, transport, job_id):
        # Same name and same input every time: exactly what Resubmit sends.
        return submit_to_runner(
            transport,
            self.host,
            make_preset(),
            make_job(id=job_id, name="opt", remote_dir="", remote_job_id=""),
            [self.input],
        )

    def test_it_gets_a_directory_of_its_own(self):
        first = self.submit(FakeTransport(self.host), "aaaaaaaaaaaa")
        second = self.submit(FakeTransport(self.host), "bbbbbbbbbbbb")

        self.assertNotEqual(first.remote_dir, second.remote_dir)

    def test_nothing_is_uploaded_over_the_previous_run(self):
        transport = FakeTransport(self.host)
        first = self.submit(transport, "aaaaaaaaaaaa")
        written_first = set(transport.uploaded_text)

        second = self.submit(transport, "bbbbbbbbbbbb")
        written_second = set(transport.uploaded_text) - written_first

        # Nothing of the second run lands anywhere under the first job's dir.
        self.assertTrue(written_second)
        self.assertFalse([p for p in written_second if p.startswith(first.remote_dir)])
        self.assertTrue([p for p in written_second if p.startswith(second.remote_dir)])

    def test_the_old_queue_entry_is_not_reused(self):
        transport = FakeTransport(self.host)
        first = self.submit(transport, "aaaaaaaaaaaa")
        second = self.submit(transport, "bbbbbbbbbbbb")

        self.assertNotEqual(first.remote_job_id, second.remote_job_id)

    def test_the_helper_having_stopped_does_not_restart_the_numbering(self):
        # The counter lives on the host, not in the helper, so a batch that
        # ended and let the helper exit still hands out the next number.
        transport = FakeTransport(self.host)
        first = self.submit(transport, "aaaaaaaaaaaa")
        second = self.submit(transport, "bbbbbbbbbbbb")

        self.assertLess(
            remote_runner.parse_entry(first.remote_job_id)[0],
            remote_runner.parse_entry(second.remote_job_id)[0],
        )

    def test_the_plugin_never_removes_a_job_directory(self):
        # Cancelling, removing a row or clearing the list touch the plugin's
        # own records; the remote directory is the only way back to results
        # still on the cluster.
        transport = FakeTransport(self.host)
        job = self.submit(transport, "aaaaaaaaaaaa")
        from job_manager.runner import cancel_in_runner

        transport.commands.clear()
        cancel_in_runner(transport, self.host, job)

        for command in transport.commands:
            self.assertNotIn(job.remote_dir, command)
            self.assertNotIn("rm -rf", command)
