"""Running at most N jobs at a time on a host that has no queue.

``nohup`` is not a scheduler. Nothing on a machine without a queue stops five
submissions starting at once and fighting over the same cores, and the plugin's
only serialisation was a single opt-in chain -- so it was strictly one at a
time, or a free-for-all, with nothing in between.

A host may now say "run at most N at a time". The limit is enforced with the
dependency machinery that already exists: submissions over the limit join the
*shortest* lane, so the waiting happens on the host and holds with MoleditPy
closed. No daemon, no remote state.
"""

import shutil
import tempfile
import unittest

from job_manager.models import (
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    STATE_RUNNING,
    HostProfile,
    Job,
)
from job_manager.store import JobStore


def make_job(**kwargs) -> Job:
    defaults = {"host_id": "h1", "scheduler": "shell", "state": STATE_RUNNING}
    defaults.update(kwargs)
    return Job(**defaults)


class SlotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="slot_limit_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(self.tmp)

    def submit(self, name: str, limit: int, **kwargs) -> Job:
        """Add a job the way the dialog would: behind whatever the limit says."""
        predecessor = self.store.chain_lane_tail("h1", limit)
        job = make_job(
            name=name,
            state=STATE_PENDING,
            after_job_id=predecessor.id if predecessor else "",
            chain_any=True,
            submitted_at=len(self.store.jobs) + 1.0,
            **kwargs,
        )
        self.store.jobs[job.id] = job
        return job


class TestLanes(SlotTestCase):
    def test_independent_jobs_are_each_their_own_lane(self):
        for name in ("a", "b", "c"):
            self.store.jobs[name] = make_job(id=name, name=name)

        self.assertEqual(len(self.store.chain_lanes("h1")), 3)

    def test_a_chain_is_one_lane_in_submission_order(self):
        first = make_job(id="a", name="a")
        second = make_job(id="b", name="b", after_job_id="a")
        third = make_job(id="c", name="c", after_job_id="b")
        self.store.jobs = {j.id: j for j in (first, second, third)}

        lanes = self.store.chain_lanes("h1")

        self.assertEqual([[job.name for job in lane] for lane in lanes], [["a", "b", "c"]])

    def test_finished_jobs_leave_the_lane(self):
        done = make_job(id="a", name="a", state=STATE_DONE)
        running = make_job(id="b", name="b", after_job_id="a")
        self.store.jobs = {done.id: done, running.id: running}

        lanes = self.store.chain_lanes("h1")

        self.assertEqual([[job.name for job in lane] for lane in lanes], [["b"]])

    def test_a_stranded_job_is_not_occupying_a_slot(self):
        # It will never run, so counting it would permanently cost the host a
        # slot it is not using.
        dead = make_job(id="a", name="a", scheduler="slurm", state=STATE_FAILED)
        stranded = make_job(id="b", name="b", scheduler="slurm", after_job_id="a")
        self.store.jobs = {dead.id: dead, stranded.id: stranded}

        self.assertEqual(self.store.chain_lanes("h1"), [])

    def test_other_hosts_are_not_counted(self):
        self.store.jobs["a"] = make_job(id="a", name="a")
        self.store.jobs["b"] = make_job(id="b", name="b", host_id="h2")

        self.assertEqual(len(self.store.chain_lanes("h1")), 1)


class TestTheLimitIsRespected(SlotTestCase):
    def test_no_limit_never_chains(self):
        for name in "abcde":
            self.submit(name, limit=0)

        self.assertEqual(len(self.store.chain_lanes("h1")), 5)

    def test_jobs_up_to_the_limit_start_straight_away(self):
        jobs = [self.submit(name, limit=3) for name in "abc"]

        self.assertEqual([job.after_job_id for job in jobs], ["", "", ""])
        # ...and the third one has just used the last slot.
        self.assertFalse(self.store.free_slot("h1", 3))

    def test_the_limit_is_never_exceeded(self):
        for name in "abcdefg":
            self.submit(name, limit=2)

        self.assertEqual(len(self.store.chain_lanes("h1")), 2)
        self.assertFalse(self.store.free_slot("h1", 2))

    def test_the_lanes_stay_balanced(self):
        # Seven jobs at a limit of two should be two queues of three and four,
        # not one queue of six behind a single job.
        for name in "abcdefg":
            self.submit(name, limit=2)

        depths = sorted(len(lane) for lane in self.store.chain_lanes("h1"))
        self.assertEqual(depths, [3, 4])

    def test_a_freed_slot_is_used_by_the_next_submission(self):
        first = self.submit("a", limit=2)
        self.submit("b", limit=2)
        self.assertFalse(self.store.free_slot("h1", 2))

        first.touch(STATE_DONE)

        self.assertTrue(self.store.free_slot("h1", 2))
        self.assertEqual(self.submit("c", limit=2).after_job_id, "")

    def test_a_queued_job_inherits_the_slot_rather_than_freeing_it(self):
        # Finishing the job at the head of a lane does not open a slot: the
        # job queued behind it takes that lane over. Otherwise a limit of two
        # would drift upwards every time something finished.
        first = self.submit("a", limit=2)
        self.submit("b", limit=2)
        self.submit("c", limit=2)  # queued behind one of the two
        first.touch(STATE_DONE)

        self.assertEqual(len(self.store.chain_lanes("h1")), 2)
        self.assertFalse(self.store.free_slot("h1", 2))

    def test_a_limit_of_one_is_a_single_chain(self):
        jobs = [self.submit(name, limit=1) for name in "abc"]

        self.assertEqual(len(self.store.chain_lanes("h1")), 1)
        self.assertEqual(jobs[1].after_job_id, jobs[0].id)
        self.assertEqual(jobs[2].after_job_id, jobs[1].id)

    def test_an_empty_host_always_has_a_free_slot(self):
        self.assertTrue(self.store.free_slot("h1", 1))
        self.assertIsNone(self.store.chain_lane_tail("h1", 1))

    def test_no_limit_means_always_free(self):
        for name in "abcde":
            self.submit(name, limit=0)

        self.assertTrue(self.store.free_slot("h1", 0))
        self.assertIsNone(self.store.chain_lane_tail("h1", 0))


class TestTheHostCarriesTheLimit(SlotTestCase):
    def test_it_defaults_to_no_limit(self):
        self.assertEqual(HostProfile().max_concurrent, 0)

    def test_it_survives_a_save_and_load(self):
        host = HostProfile(name="workstation", max_concurrent=4)
        self.store.hosts = {host.id: host}
        self.store.save_settings()

        reloaded = JobStore(self.tmp)

        self.assertEqual(reloaded.hosts[host.id].max_concurrent, 4)

    def test_a_profile_written_before_the_field_existed_still_loads(self):
        raw = HostProfile(name="old").to_dict()
        del raw["max_concurrent"]

        self.assertEqual(HostProfile.from_dict(raw).max_concurrent, 0)


if __name__ == "__main__":
    unittest.main()
