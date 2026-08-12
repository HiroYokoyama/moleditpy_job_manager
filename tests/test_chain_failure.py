"""What happens to a chain when one of its jobs fails.

SLURM and PBS hold a chained job with ``afterok``, so a predecessor that fails
or is cancelled leaves everything behind it queued for ever -- and the queue
goes on reporting PENDING, which reads as "starting soon". SGE's ``-hold_jid``
and the no-queue wrapper's ``kill -0`` both release on the predecessor merely
ending, so the same checkbox means two different things depending on the host.

These tests pin down which schedulers strand a chain, that the plugin says so
rather than showing PENDING, and that ``afterany`` is available for jobs that
are only being serialised to share a machine.
"""

import shutil
import tempfile
import unittest

from job_manager.models import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    STATE_RUNNING,
    Job,
    SubmitPreset,
)
from job_manager.schedulers import get_scheduler
from job_manager.store import JobStore


def make_job(**kwargs) -> Job:
    defaults = {"host_id": "h1", "scheduler": "slurm", "state": STATE_PENDING}
    defaults.update(kwargs)
    return Job(**defaults)


class TestDependencyFlavour(unittest.TestCase):
    """afterok by default, afterany on request."""

    def test_slurm_holds_on_success_by_default(self):
        directives = get_scheduler("slurm").dependency_directives("4242")
        self.assertEqual(directives, ["#SBATCH --dependency=afterok:4242"])

    def test_slurm_can_be_asked_to_hold_on_any_outcome(self):
        directives = get_scheduler("slurm").dependency_directives("4242", any_outcome=True)
        self.assertEqual(directives, ["#SBATCH --dependency=afterany:4242"])

    def test_pbs_holds_on_success_by_default(self):
        directives = get_scheduler("pbs").dependency_directives("77.head")
        self.assertEqual(directives, ["#PBS -W depend=afterok:77.head"])

    def test_pbs_can_be_asked_to_hold_on_any_outcome(self):
        directives = get_scheduler("pbs").dependency_directives("77.head", any_outcome=True)
        self.assertEqual(directives, ["#PBS -W depend=afterany:77.head"])

    def test_sge_has_only_one_form(self):
        # -hold_jid releases on completion whatever the exit status, so asking
        # for afterany must not invent a flag SGE would silently ignore.
        scheduler = get_scheduler("sge")
        self.assertEqual(
            scheduler.dependency_directives("99"),
            scheduler.dependency_directives("99", any_outcome=True),
        )

    def test_an_invalid_job_id_is_never_put_into_a_directive(self):
        for name in ("slurm", "pbs", "sge"):
            with self.subTest(scheduler=name):
                evil = "1234; rm -rf ~"
                self.assertEqual(get_scheduler(name).dependency_directives(evil), [])
                self.assertEqual(
                    get_scheduler(name).dependency_directives(evil, any_outcome=True), []
                )

    def test_which_schedulers_strand_a_chain(self):
        stranding = {
            name: not get_scheduler(name).chain_releases_on_failure
            for name in ("slurm", "pbs", "sge", "shell")
        }
        self.assertEqual(stranding, {"slurm": True, "pbs": True, "sge": False, "shell": False})


class TestScriptCarriesTheChoice(unittest.TestCase):
    """The flag has to reach the generated script, not just the scheduler."""

    def _script(self, scheduler: str, any_outcome: bool) -> str:
        return get_scheduler(scheduler).build_script(
            "job",
            SubmitPreset(command_template="orca {input}"),
            "in.inp",
            "job.log",
            run_after="4242",
            run_after_any=any_outcome,
            remote_dir="/scratch/job",
        )

    def test_default_script_asks_for_afterok(self):
        self.assertIn("--dependency=afterok:4242", self._script("slurm", False))

    def test_requested_script_asks_for_afterany(self):
        script = self._script("slurm", True)
        self.assertIn("--dependency=afterany:4242", script)
        self.assertNotIn("afterok", script)

    def test_the_directive_still_precedes_the_first_command(self):
        # A queue stops reading directives at the first executable line, and a
        # dependency it never read is a chain that does not exist.
        script = self._script("slurm", True)
        lines = script.splitlines()
        dependency = next(i for i, line in enumerate(lines) if "afterany" in line)
        first_command = next(
            i for i, line in enumerate(lines) if line.startswith(("cd ", "rm -f", "trap "))
        )
        self.assertLess(dependency, first_command)


class StoreChainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chain_failure_")
        self.store = JobStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBlockedDetection(StoreChainTestCase):
    def _pair(self, scheduler="slurm", predecessor_state=STATE_FAILED, chain_any=False):
        first = make_job(name="opt", scheduler=scheduler, state=predecessor_state)
        second = make_job(
            name="freq", scheduler=scheduler, after_job_id=first.id, chain_any=chain_any
        )
        self.store.jobs = {first.id: first, second.id: second}
        return first, second

    def test_a_failed_predecessor_blocks_the_job_behind_it(self):
        first, second = self._pair()
        self.assertIs(self.store.chain_blocker(second), first)

    def test_a_cancelled_predecessor_blocks_it_too(self):
        first, second = self._pair(predecessor_state=STATE_CANCELLED)
        self.assertIs(self.store.chain_blocker(second), first)

    def test_a_successful_predecessor_blocks_nothing(self):
        _, second = self._pair(predecessor_state=STATE_DONE)
        self.assertIsNone(self.store.chain_blocker(second))

    def test_a_running_predecessor_blocks_nothing(self):
        _, second = self._pair(predecessor_state=STATE_RUNNING)
        self.assertIsNone(self.store.chain_blocker(second))

    def test_afterany_is_never_blocked(self):
        _, second = self._pair(chain_any=True)
        self.assertIsNone(self.store.chain_blocker(second))

    def test_sge_releases_so_nothing_is_blocked(self):
        _, second = self._pair(scheduler="sge")
        self.assertIsNone(self.store.chain_blocker(second))

    def test_the_no_queue_wrapper_releases_too(self):
        _, second = self._pair(scheduler="shell")
        self.assertIsNone(self.store.chain_blocker(second))

    def test_a_terminal_job_is_not_blocked_by_anything(self):
        first, second = self._pair()
        second.state = STATE_DONE
        self.assertIsNone(self.store.chain_blocker(second))

    def test_a_missing_predecessor_blocks_nothing(self):
        job = make_job(after_job_id="gone")
        self.store.jobs = {job.id: job}
        self.assertIsNone(self.store.chain_blocker(job))

    def test_an_unknown_scheduler_does_not_raise(self):
        first, second = self._pair()
        second.scheduler = "torque-from-2004"
        self.assertIsNone(self.store.chain_blocker(second))

    def test_dependents_are_found_by_predecessor_id(self):
        first, second = self._pair()
        self.assertEqual([job.id for job in self.store.dependents_of(first.id)], [second.id])
        self.assertEqual(self.store.dependents_of(second.id), [])


class TestChainTailSkipsDeadChains(StoreChainTestCase):
    def test_a_new_job_does_not_queue_behind_a_stranded_one(self):
        # Joining a chain that is already dead would strand the new job with
        # it, which is never what pressing Submit means.
        dead = make_job(name="opt", state=STATE_FAILED, submitted_at=100.0)
        stranded = make_job(name="freq", after_job_id=dead.id, submitted_at=200.0)
        running = make_job(name="other", state=STATE_RUNNING, submitted_at=150.0)
        self.store.jobs = {j.id: j for j in (dead, stranded, running)}

        self.assertIs(self.store.chain_tail("h1"), running)

    def test_the_newest_runnable_job_is_still_the_tail(self):
        first = make_job(name="a", state=STATE_RUNNING, submitted_at=100.0)
        second = make_job(name="b", after_job_id=first.id, submitted_at=200.0)
        self.store.jobs = {first.id: first, second.id: second}

        self.assertIs(self.store.chain_tail("h1"), second)

    def test_nothing_runnable_means_no_chaining(self):
        dead = make_job(name="opt", state=STATE_FAILED, submitted_at=100.0)
        stranded = make_job(name="freq", after_job_id=dead.id, submitted_at=200.0)
        self.store.jobs = {dead.id: dead, stranded.id: stranded}

        self.assertIsNone(self.store.chain_tail("h1"))


class TestChainAnySurvivesTheRoundTrip(StoreChainTestCase):
    def test_the_flag_is_persisted_and_read_back(self):
        job = make_job(name="freq", after_job_id="abc", chain_any=True)
        self.store.jobs = {job.id: job}
        self.store.save_jobs()

        reloaded = JobStore(self.tmp)
        self.assertTrue(reloaded.jobs[job.id].chain_any)

    def test_a_job_list_written_before_the_flag_existed_still_loads(self):
        job = make_job(name="freq", after_job_id="abc")
        raw = job.to_dict()
        del raw["chain_any"]
        self.assertFalse(Job.from_dict(raw).chain_any)


if __name__ == "__main__":
    unittest.main()
