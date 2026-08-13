"""A failure strands everything behind it, not just the next job along.

BLOCKED exists because SLURM and PBS report a stranded job as PENDING for
ever, which reads as "starting soon" and is the opposite of the truth. Only
the job immediately behind the failure used to get that treatment.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest

from job_manager.models import (
    SCHEDULER_SGE,
    SCHEDULER_SLURM,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    STATE_RUNNING,
    Job,
)
from job_manager.store import JobStore


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chain_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(self.tmp)

    def add(self, job_id, state=STATE_PENDING, after="", scheduler=SCHEDULER_SLURM, **kwargs):
        job = Job(
            id=job_id,
            name=job_id,
            host_id="h",
            scheduler=scheduler,
            state=state,
            after_job_id=after,
            **kwargs,
        )
        self.store.jobs[job.id] = job
        return job

    def chain(self, length=3, head_state=STATE_FAILED, **kwargs):
        """A -> B -> C, with A in ``head_state``."""
        names = ["A", "B", "C", "D"][:length]
        jobs = [self.add(names[0], state=head_state, **kwargs)]
        for name, previous in zip(names[1:], names):
            jobs.append(self.add(name, after=previous, **kwargs))
        return jobs


class TestBlockingReachesTheWholeChain(ChainTestCase):
    def test_the_job_behind_a_failure_is_blocked(self):
        _a, b = self.chain(2)
        self.assertIsNotNone(self.store.chain_blocker(b))

    def test_the_job_behind_that_one_is_blocked_too(self):
        # It is exactly as dead: B never starts, so it never ends.
        _a, _b, c = self.chain(3)
        self.assertIsNotNone(self.store.chain_blocker(c))

    def test_the_blocker_named_is_the_job_that_actually_died(self):
        # "waits for B, which is pending" would be true and useless.
        _a, _b, c = self.chain(3)
        self.assertEqual(self.store.chain_blocker(c).id, "A")

    def test_a_healthy_chain_blocks_nothing(self):
        _a, b, c = self.chain(3, head_state=STATE_RUNNING)
        self.assertIsNone(self.store.chain_blocker(b))
        self.assertIsNone(self.store.chain_blocker(c))

    def test_a_succeeded_predecessor_blocks_nothing(self):
        _a, b, c = self.chain(3, head_state=STATE_DONE)
        self.assertIsNone(self.store.chain_blocker(b))
        self.assertIsNone(self.store.chain_blocker(c))

    def test_a_cancelled_predecessor_strands_the_chain_as_well(self):
        _a, _b, c = self.chain(3, head_state=STATE_CANCELLED)
        self.assertIsNotNone(self.store.chain_blocker(c))

    def test_a_scheduler_that_releases_on_failure_never_blocks(self):
        # SGE's -hold_jid releases on completion, whatever the outcome.
        _a, _b, c = self.chain(3, scheduler=SCHEDULER_SGE)
        self.assertIsNone(self.store.chain_blocker(c))

    def test_a_terminal_job_is_never_reported_blocked(self):
        self.add("A", state=STATE_FAILED)
        done = self.add("B", state=STATE_DONE, after="A")
        self.assertIsNone(self.store.chain_blocker(done))


class TestChainAnyIsReadAtTheRightLink(ChainTestCase):
    def test_an_afterany_job_behind_a_failure_runs(self):
        self.add("A", state=STATE_FAILED)
        b = self.add("B", after="A", chain_any=True)
        self.assertIsNone(self.store.chain_blocker(b))

    def test_an_afterany_job_behind_a_blocked_one_is_still_blocked(self):
        # The subtle case: C's own dependency is loose, but it waits for B --
        # and B never starts, so it never ends, so afterany never releases.
        self.add("A", state=STATE_FAILED)
        self.add("B", after="A")
        c = self.add("C", after="B", chain_any=True)
        self.assertIsNotNone(self.store.chain_blocker(c))

    def test_a_loose_link_further_back_saves_the_whole_chain(self):
        self.add("A", state=STATE_FAILED)
        self.add("B", after="A", chain_any=True)
        c = self.add("C", after="B")
        self.assertIsNone(self.store.chain_blocker(c))


class TestSlotAccounting(ChainTestCase):
    """A dead chain used to hold a host's slot for the rest of the session."""

    def test_a_stranded_chain_is_not_a_running_lane(self):
        self.chain(3)
        self.assertEqual(self.store.chain_lanes("h"), [])

    def test_its_slot_is_given_back(self):
        self.chain(3)
        self.assertTrue(self.store.free_slot("h", 1))

    def test_a_live_chain_still_holds_its_slot(self):
        self.chain(3, head_state=STATE_RUNNING)
        self.assertFalse(self.store.free_slot("h", 1))

    def test_nothing_queues_behind_a_dead_chain(self):
        # Joining a stranded chain strands the new job with it.
        self.chain(3)
        self.assertIsNone(self.store.chain_tail("h"))

    def test_a_cycle_does_not_hang(self):
        # A job list is a file and can be opened by drag and drop from
        # anywhere; walking a cycle for ever is a frozen application.
        a = self.add("A")
        b = self.add("B", after="A")
        a.after_job_id = "B"

        self.assertIsNone(self.store.chain_blocker(b))
        self.assertIsNone(self.store.chain_blocker(a))


class TestDependentsOf(ChainTestCase):
    def test_direct_dependents_by_default(self):
        self.chain(3)
        self.assertEqual([j.id for j in self.store.dependents_of("A")], ["B"])

    def test_the_whole_chain_when_asked(self):
        self.chain(3)
        self.assertEqual(
            sorted(j.id for j in self.store.dependents_of("A", recursive=True)), ["B", "C"]
        )

    def test_a_cycle_terminates_without_returning_the_job_itself(self):
        a = self.add("A")
        self.add("B", after="A")
        a.after_job_id = "B"

        found = self.store.dependents_of("A", recursive=True)

        self.assertEqual([j.id for j in found], ["B"])

    def test_a_job_with_nothing_behind_it(self):
        self.add("A", state=STATE_FAILED)
        self.assertEqual(self.store.dependents_of("A", recursive=True), [])


class TestForgottenJobsAreScopedToTheirList(unittest.TestCase):
    """Removals belong to the list they were made in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="forget_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_switching_lists_does_not_carry_a_removal_across(self):
        import json
        import os

        store = JobStore(self.tmp)
        store.jobs["shared"] = Job(id="shared", name="x")
        store.save_jobs()
        store.remove_job("shared")

        other = os.path.join(self.tmp, "other.pmejbs")
        with open(other, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "jobs": [{"id": "shared", "name": "x"}]}, handle)

        store.use_jobs_file(other)
        store.save_jobs()

        with open(other, encoding="utf-8") as handle:
            self.assertEqual([raw["id"] for raw in json.load(handle)["jobs"]], ["shared"])


if __name__ == "__main__":
    unittest.main()
