"""Submitting to, polling and cancelling in the remote runner.

``test_remote_runner.py`` proves the runner script behaves; this proves the
plugin drives it correctly -- the order of the commands, which is where the
safety lives. Enqueueing before starting a runner, and moving into ``queue/``
rather than uploading into it, are not details: get either backwards and jobs
are silently dropped.
"""

import os
import shutil
import tempfile
import unittest

from job_manager import remote_runner
from job_manager.models import (
    MODE_LANES,
    MODE_RUNNER,
    SCHEDULER_SHELL,
    SCHEDULER_SLURM,
    STATE_PENDING,
    STATE_RUNNING,
    STATE_SUBMITTED,
    HostProfile,
    Job,
)
from job_manager.runner import cancel_in_runner, poll_runner, submit_to_runner

from .fakes import FakeTransport, make_preset


def make_host(**kwargs) -> HostProfile:
    defaults = {
        "name": "workstation",
        "hostname": "ws",
        "username": "me",
        "scheduler": SCHEDULER_SHELL,
        "concurrency_mode": MODE_RUNNER,
        "max_concurrent": 2,
        "remote_root": "~/moleditpy_jobs",
    }
    defaults.update(kwargs)
    return HostProfile(**defaults)


class RunnerModeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="runner_mode_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = os.path.join(self.tmp, "mol.inp")
        with open(self.input, "w", encoding="utf-8") as handle:
            handle.write("! B3LYP\n")
        self.host = make_host()
        self.transport = FakeTransport(self.host)
        self.directory = remote_runner.runner_dir(self.host.remote_root)

    def submit(self, name="opt", **kwargs) -> Job:
        job = Job(name=name, host_id=self.host.id, scheduler=SCHEDULER_SHELL, **kwargs)
        return submit_to_runner(self.transport, self.host, make_preset(), job, [self.input], **{})

    def index_of(self, needle: str) -> int:
        for index, command in enumerate(self.transport.commands):
            if needle in command:
                return index
        self.fail(f"no command contained {needle!r}; ran {self.transport.commands}")


class TestWhichHostsUseIt(unittest.TestCase):
    def test_a_shell_host_in_runner_mode_uses_it(self):
        self.assertTrue(make_host().uses_remote_runner)

    def test_lane_mode_does_not(self):
        self.assertFalse(make_host(concurrency_mode=MODE_LANES).uses_remote_runner)

    def test_a_real_queue_never_does(self):
        # A cluster already has a scheduler; a second one on the login node is
        # both pointless and the thing sysadmins object to.
        self.assertFalse(make_host(scheduler=SCHEDULER_SLURM).uses_remote_runner)

    def test_lanes_are_the_default(self):
        self.assertEqual(HostProfile().concurrency_mode, MODE_LANES)
        self.assertFalse(HostProfile().uses_remote_runner)

    def test_a_profile_written_before_the_field_existed_still_loads(self):
        raw = HostProfile(name="old").to_dict()
        del raw["concurrency_mode"]
        del raw["runner_cores"]

        restored = HostProfile.from_dict(raw)

        self.assertEqual(restored.concurrency_mode, MODE_LANES)
        self.assertEqual(restored.runner_cores, 0)


class TestSubmitting(RunnerModeTestCase):
    def test_the_job_is_queued_and_marked_submitted(self):
        job = self.submit()

        self.assertEqual(job.state, STATE_SUBMITTED)
        self.assertTrue(job.remote_job_id.endswith(f"_{job.id}.sh"))

    def test_the_wrapper_is_uploaded_to_the_job_directory(self):
        job = self.submit()

        wrapper = f"{job.remote_dir}/moleditpy_run.sh"
        self.assertIn(wrapper, self.transport.uploaded_text)
        # The same wrapper as the no-queue scheduler builds: same sentinel,
        # same signal traps, so completion is detected the usual way.
        self.assertIn(".moleditpy_rc", self.transport.uploaded_text[wrapper])

    def test_the_queued_script_goes_through_tmp(self):
        job = self.submit()

        uploaded = f"{self.directory}/tmp/{job.remote_job_id}"
        self.assertIn(uploaded, self.transport.uploaded_text)

    def test_it_is_moved_into_the_queue_not_uploaded_into_it(self):
        # Uploading straight into queue/ would let the runner start a
        # half-written script.
        job = self.submit()

        self.assertNotIn(
            f"{self.directory}/queue/{job.remote_job_id}", self.transport.uploaded_text
        )
        self.assertLess(
            self.index_of(f'mv "tmp/{job.remote_job_id}"'),
            len(self.transport.commands),
        )

    def test_the_runner_is_started_only_after_the_job_is_queued(self):
        # The other order is a silent dropped job: a runner started first can
        # empty the queue and exit before the job arrives.
        job = self.submit()

        self.assertLess(
            self.index_of(f'mv "tmp/{job.remote_job_id}"'),
            self.index_of("mkdir lock"),
        )

    def test_the_limits_are_written_before_the_runner_starts(self):
        self.submit()

        self.assertLess(self.index_of("> slots"), self.index_of("mkdir lock"))

    def test_the_job_asks_for_the_presets_cpus(self):
        preset = make_preset()
        preset.cpus_per_task = 8
        job = Job(name="wide", host_id=self.host.id, scheduler=SCHEDULER_SHELL)

        submit_to_runner(self.transport, self.host, preset, job, [self.input])

        script = self.transport.uploaded_text[f"{self.directory}/tmp/{job.remote_job_id}"]
        self.assertIn(f"{remote_runner.CORES_TAG} 8", script)

    def test_a_chained_job_carries_its_dependency_as_a_header(self):
        first = self.submit(name="opt")
        second = Job(name="freq", host_id=self.host.id, scheduler=SCHEDULER_SHELL)

        submit_to_runner(
            self.transport, self.host, make_preset(), second, [self.input], after_job=first
        )

        script = self.transport.uploaded_text[f"{self.directory}/tmp/{second.remote_job_id}"]
        self.assertIn(f"{remote_runner.AFTER_TAG} {first.id}", script)
        self.assertIn(f"{remote_runner.REQUIRE_SUCCESS_TAG} 1", script)

    def test_chain_any_asks_the_runner_not_to_require_success(self):
        first = self.submit(name="opt")
        second = Job(name="freq", host_id=self.host.id, scheduler=SCHEDULER_SHELL, chain_any=True)

        submit_to_runner(
            self.transport, self.host, make_preset(), second, [self.input], after_job=first
        )

        script = self.transport.uploaded_text[f"{self.directory}/tmp/{second.remote_job_id}"]
        self.assertIn(f"{remote_runner.REQUIRE_SUCCESS_TAG} 0", script)

    def test_queue_numbers_do_not_repeat(self):
        self.transport.when("for d in queue running done", stdout="done job_0004_old.sh\n")
        job = Job(name="next", host_id=self.host.id, scheduler=SCHEDULER_SHELL)

        submit_to_runner(self.transport, self.host, make_preset(), job, [self.input])

        self.assertEqual(remote_runner.parse_entry(job.remote_job_id)[0], 5)

    def test_no_input_file_is_refused(self):
        job = Job(name="empty", host_id=self.host.id, scheduler=SCHEDULER_SHELL)

        with self.assertRaises(ValueError):
            submit_to_runner(self.transport, self.host, make_preset(), job, [])


class TestPolling(RunnerModeTestCase):
    def _job(self, entry_number=1, state=STATE_SUBMITTED) -> Job:
        job = Job(name="opt", host_id=self.host.id, scheduler=SCHEDULER_SHELL, state=state)
        job.remote_job_id = remote_runner.entry_name(entry_number, job.id)
        job.remote_dir = "/home/me/jobs/opt"
        return job

    def test_a_waiting_job_reads_as_pending(self):
        job = self._job()
        self.transport.when("for d in queue running done", stdout=f"queue {job.remote_job_id}\n")

        self.assertEqual(poll_runner(self.transport, self.host, [job]), {job.id: STATE_PENDING})

    def test_a_dispatched_job_reads_as_running(self):
        job = self._job()
        self.transport.when("for d in queue running done", stdout=f"running {job.remote_job_id}\n")

        self.assertEqual(poll_runner(self.transport, self.host, [job]), {job.id: STATE_RUNNING})

    def test_an_unchanged_state_is_not_reported(self):
        job = self._job(state=STATE_RUNNING)
        self.transport.when("for d in queue running done", stdout=f"running {job.remote_job_id}\n")

        self.assertEqual(poll_runner(self.transport, self.host, [job]), {})

    def test_a_finished_job_is_resolved_from_its_own_sentinel(self):
        # The runner's opinion is not consulted for the exit code: the wrapper
        # wrote the real one, the same as on every other backend.
        job = self._job()
        self.transport.when("for d in queue running done", stdout=f"done {job.remote_job_id}\n")
        self.transport.when("@@MOLEDITPY@@", stdout="@@MOLEDITPY@@\n0\n")

        updates = poll_runner(self.transport, self.host, [job])

        self.assertEqual(updates, {job.id: "DONE"})
        self.assertEqual(job.rc, 0)

    def test_a_job_the_runner_blocked_is_reported_as_failed_with_a_reason(self):
        job = self._job()
        self.transport.when("for d in queue running done", stdout=f"done {job.remote_job_id}\n")
        self.transport.when("status/", stdout=f"@@MOLEDITPY@@\n{remote_runner.STATUS_BLOCKED}\n")
        self.transport.when("@@MOLEDITPY@@", stdout="@@MOLEDITPY@@\nMISSING\n")

        updates = poll_runner(self.transport, self.host, [job])

        self.assertEqual(updates, {job.id: "FAILED"})
        self.assertIn("never started", job.last_error)

    def test_nothing_tracked_costs_no_round_trip(self):
        self.assertEqual(poll_runner(self.transport, self.host, []), {})
        self.assertEqual(self.transport.commands, [])

    def test_one_listing_covers_every_job(self):
        jobs = [self._job(1), self._job(2)]
        self.transport.when("for d in queue running done", stdout="")
        self.transport.when("@@MOLEDITPY@@", stdout="@@MOLEDITPY@@\n0\n@@MOLEDITPY@@\n0\n")

        poll_runner(self.transport, self.host, jobs)

        listings = [c for c in self.transport.commands if "for d in queue running done" in c]
        self.assertEqual(len(listings), 1)


class TestCancelling(RunnerModeTestCase):
    def test_it_asks_the_runner_to_cancel_by_entry(self):
        job = Job(name="opt", host_id=self.host.id, scheduler=SCHEDULER_SHELL)
        job.remote_job_id = remote_runner.entry_name(3, job.id)

        cancel_in_runner(self.transport, self.host, job)

        command = self.transport.commands[-1]
        # A waiting job is cancelled by leaving the queue, which frees its slot
        # at once; only a running one needs killing.
        self.assertIn(f'mv "queue/{job.remote_job_id}"', command)
        self.assertIn("kill", command)


if __name__ == "__main__":
    unittest.main()
