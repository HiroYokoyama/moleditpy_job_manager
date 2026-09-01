"""Two Job Managers, one job file.

A second MoleditPy window, or the standalone monitor beside the plugin, holds
its own copy of the whole job list. Saving merges (see ``_merged_jobs``), but
nothing ever read the other side's writes back, so a job submitted or finished
over there stayed invisible here until the next restart. These are the rules
Reload List follows -- and, in particular, the two things it must not touch.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest

from job_manager.models import (
    STATE_DONE,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_UPLOADING,
    Job,
)
from job_manager.store import JobStore


def make_job(job_id: str, **overrides) -> Job:
    fields = dict(id=job_id, name=job_id, host_id="h1", host_name="host", updated_at=1000.0)
    fields.update(overrides)
    return Job(**fields)


class ReloadTestCase(unittest.TestCase):
    """Two stores over one directory, which is exactly two instances."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reload_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mine = JobStore(self.tmp)
        self.theirs = JobStore(self.tmp)

    def their_jobs(self, *jobs: Job) -> None:
        """Let the other instance own exactly these jobs, and save."""
        self.theirs.jobs = {job.id: job for job in jobs}
        self.theirs.save_jobs()


class TestWhatArrives(ReloadTestCase):
    def test_a_job_the_other_instance_submitted_arrives(self):
        self.their_jobs(make_job("j1"))
        result = self.mine.reload_jobs()
        self.assertEqual((result.added, result.updated, result.removed), (1, 0, 0))
        self.assertIn("j1", self.mine.jobs)

    def test_a_newer_record_wins(self):
        self.mine.add_job(make_job("j1", state=STATE_RUNNING, updated_at=1000.0))
        self.their_jobs(make_job("j1", state=STATE_DONE, updated_at=2000.0))
        result = self.mine.reload_jobs()
        self.assertEqual(result.updated, 1)
        self.assertEqual(self.mine.jobs["j1"].state, STATE_DONE)

    def test_a_staler_record_does_not(self):
        # Our poll was the more recent one; the file must not undo it.
        self.mine.add_job(make_job("j1", state=STATE_DONE, updated_at=3000.0))
        self.their_jobs(make_job("j1", state=STATE_RUNNING, updated_at=1000.0))
        result = self.mine.reload_jobs()
        self.assertEqual(result.updated, 0)
        self.assertEqual(self.mine.jobs["j1"].state, STATE_DONE)

    def test_a_job_removed_elsewhere_is_dropped(self):
        # Through the other instance's own Remove, not by writing a shorter
        # file: its save would otherwise re-adopt a job it had never heard of,
        # which is _merged_jobs doing its job.
        self.mine.add_job(make_job("j1"))
        self.mine.add_job(make_job("j2"))
        self.theirs.reload_jobs()
        self.theirs.remove_job("j2")
        result = self.mine.reload_jobs()
        self.assertEqual(result.removed, 1)
        self.assertEqual(set(self.mine.jobs), {"j1"})

    def test_a_job_we_removed_on_purpose_does_not_come_back(self):
        # _forgotten is what stops our own save re-adopting it; a reload has to
        # honour the same list or Remove would undo itself on the next click.
        self.mine.add_job(make_job("j1"))
        self.mine.remove_job("j1")
        self.their_jobs(make_job("j1"))
        result = self.mine.reload_jobs()
        self.assertEqual(result.added, 0)
        self.assertNotIn("j1", self.mine.jobs)

    def test_nothing_to_do_is_reported_as_nothing(self):
        self.mine.add_job(make_job("j1"))
        result = self.mine.reload_jobs()
        self.assertEqual(result.total, 0)
        self.assertIn("already up to date", result.summary())

    def test_the_summary_names_what_happened(self):
        self.mine.add_job(make_job("j1"))
        self.theirs.reload_jobs()
        self.theirs.remove_job("j1")
        self.theirs.add_job(make_job("j2"))
        text = self.mine.reload_jobs().summary()
        self.assertIn("1 new", text)
        self.assertIn("1 removed elsewhere", text)


class TestWhatIsLeftAlone(ReloadTestCase):
    """Live work, on both sides of the file."""

    def test_a_job_we_are_uploading_is_not_overwritten(self):
        # The worker thread owns this record and is about to write the remote
        # directory and queue id onto it. The file cannot know that yet.
        self.mine.add_job(make_job("j1", state=STATE_UPLOADING, updated_at=1000.0))
        self.their_jobs(make_job("j1", state=STATE_FAILED, updated_at=5000.0))
        result = self.mine.reload_jobs()
        self.assertEqual(result.updated, 0)
        self.assertEqual(self.mine.jobs["j1"].state, STATE_UPLOADING)

    def test_a_job_we_are_downloading_is_not_overwritten(self):
        self.mine.add_job(make_job("j1", state=STATE_DOWNLOADING, updated_at=1000.0))
        self.their_jobs(make_job("j1", state=STATE_DONE, updated_at=5000.0))
        self.mine.reload_jobs()
        self.assertEqual(self.mine.jobs["j1"].state, STATE_DOWNLOADING)

    def test_a_job_we_are_uploading_is_not_dropped_when_absent(self):
        self.mine.jobs["j1"] = make_job("j1", state=STATE_UPLOADING)
        self.their_jobs()
        result = self.mine.reload_jobs()
        self.assertEqual(result.removed, 0)
        self.assertIn("j1", self.mine.jobs)

    def test_the_other_instances_upload_is_not_declared_dead(self):
        """The one that would be a data-loss bug rather than an annoyance.

        ``resolve_interrupted`` exists for a *restart*: nothing can still be
        UPLOADING once the process that was uploading has gone. Read off disk
        mid-session it means the opposite -- another instance is uploading right
        now -- so running it here would report a live submission as FAILED, and
        our next save would write that back over the truth.
        """
        self.their_jobs(make_job("j1", state=STATE_UPLOADING))
        self.mine.reload_jobs()
        self.assertEqual(self.mine.jobs["j1"].state, STATE_UPLOADING)

    def test_a_reload_does_not_settle_our_own_transfers_either(self):
        self.mine.jobs["j1"] = make_job("j1", state=STATE_DOWNLOADING)
        self.their_jobs(make_job("j2"))
        self.mine.reload_jobs()
        self.assertEqual(self.mine.jobs["j1"].state, STATE_DOWNLOADING)


class TestItReallyIsTheRoundTrip(ReloadTestCase):
    """End to end over the file, rather than over a handcrafted document."""

    def test_a_full_exchange_leaves_both_agreeing(self):
        self.mine.add_job(make_job("mine", updated_at=1000.0))
        self.theirs.reload_jobs()
        self.theirs.add_job(make_job("theirs", updated_at=1000.0))
        self.theirs.jobs["mine"].touch(STATE_DONE)
        self.theirs.save_jobs()

        self.mine.reload_jobs()
        self.assertEqual(set(self.mine.jobs), {"mine", "theirs"})
        self.assertEqual(self.mine.jobs["mine"].state, STATE_DONE)

    def test_our_save_does_not_undo_what_we_just_took_in(self):
        self.their_jobs(make_job("j1", state=STATE_DONE, updated_at=2000.0))
        self.mine.reload_jobs()
        self.mine.save_jobs()
        fresh = JobStore(self.tmp)
        self.assertEqual(fresh.jobs["j1"].state, STATE_DONE)

    def test_a_chain_reads_the_predecessor_that_arrived(self):
        # blocked_ids is memoised on a revision counter; a reload that forgot to
        # invalidate it would answer from the list as it was before.
        self.mine.add_job(make_job("second", after_job_id="first", state=STATE_RUNNING))
        blocked_before = self.mine.blocked_ids()
        self.their_jobs(
            make_job("second", after_job_id="first", state=STATE_RUNNING, updated_at=1000.0),
            make_job("first", state=STATE_FAILED, updated_at=2000.0),
        )
        self.mine.reload_jobs()
        self.assertNotIn("second", blocked_before)
        self.assertIn("second", self.mine.blocked_ids())


class TestAnUnreadableFile(ReloadTestCase):
    def test_a_missing_file_removes_nothing(self):
        # read_json answers {} for a file that is not there, which is exactly
        # what a cleared list looks like -- and reading "not written yet" as
        # "everything was removed" would empty the table over a moved file.
        self.mine.jobs["j1"] = make_job("j1", state=STATE_DONE)
        result = self.mine.reload_jobs()
        self.assertEqual(result.total, 0)
        self.assertIn("j1", self.mine.jobs)

    def test_a_list_that_really_was_cleared_is_taken(self):
        # The other half of it: an empty document on disk is a deliberate act.
        self.mine.add_job(make_job("j1", state=STATE_DONE))
        self.theirs.reload_jobs()
        self.theirs.clear_jobs()
        result = self.mine.reload_jobs()
        self.assertEqual(result.removed, 1)
        self.assertEqual(self.mine.jobs, {})

    def test_a_corrupt_file_is_not_a_crash(self):
        with open(self.mine.jobs_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.mine.jobs["j1"] = make_job("j1", state=STATE_DONE)
        self.mine.reload_jobs()

    def test_timestamps_are_real_seconds(self):
        # touch() stamps from time.time(); a record written now must beat one
        # written a moment ago, which is the whole basis of "newer wins".
        job = make_job("j1")
        job.touch(STATE_RUNNING)
        self.assertGreater(job.updated_at, time.time() - 60)


if __name__ == "__main__":
    unittest.main()
