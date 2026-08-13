"""Two ways the plugin could quietly destroy something the user wanted.

Both are about writes that land in a place the user is already working in:
results now come back beside the input file, and the job list is shared by
every MoleditPy running on the machine.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from job_manager.runner import PARTIAL_SUFFIX, fetch_results
from job_manager.store import JobStore
from job_manager.transport.base import TransportError

from .fakes import FakeTransport, make_job


class TestAnInterruptedDownload(unittest.TestCase):
    """A transfer cut off half way must not wear the name of a finished file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fetch_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.job = make_job(fetch_globs=[])
        self.transport = FakeTransport()
        self.transport.when("ls -p -1", stdout="mol.out\njob.log\n")

    def test_a_completed_download_ends_up_under_its_real_name(self):
        paths = fetch_results(self.transport, self.job, self.tmp)

        self.assertIn(os.path.join(self.tmp, "mol.out"), paths)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "mol.out")))

    def test_no_part_file_is_left_behind_by_a_success(self):
        fetch_results(self.transport, self.job, self.tmp)

        leftovers = [n for n in os.listdir(self.tmp) if n.endswith(PARTIAL_SUFFIX)]
        self.assertEqual(leftovers, [])

    def test_a_failed_download_leaves_nothing_under_the_real_name(self):
        # Before, the truncated bytes stayed on disk as mol.out -- in the
        # user's own working directory, looking exactly like a finished result.
        self.transport.fail_downloads = ("mol.out",)

        paths = fetch_results(self.transport, self.job, self.tmp)

        self.assertNotIn(os.path.join(self.tmp, "mol.out"), paths)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mol.out")))

    def test_a_failed_download_cleans_up_its_part_file(self):
        self.transport.fail_downloads = ("mol.out",)

        fetch_results(self.transport, self.job, self.tmp)

        self.assertEqual([n for n in os.listdir(self.tmp) if PARTIAL_SUFFIX in n], [])

    def test_a_failure_does_not_destroy_the_previous_good_copy(self):
        # The realistic case: a job is downloaded, then re-downloaded, and the
        # second attempt dies. Writing in place truncated the good file first.
        fetch_results(self.transport, self.job, self.tmp)
        self.transport.fail_downloads = ("mol.out",)

        fetch_results(self.transport, self.job, self.tmp)

        with open(os.path.join(self.tmp, "mol.out")) as handle:
            self.assertIn("content of", handle.read())

    def test_the_other_files_still_arrive(self):
        self.transport.fail_downloads = ("mol.out",)

        paths = fetch_results(self.transport, self.job, self.tmp)

        self.assertIn(os.path.join(self.tmp, "job.log"), paths)

    def test_a_rename_that_fails_is_not_reported_as_a_download(self):
        # os.replace can fail on its own (a directory in the way, a permission
        # problem); the file must not be counted as fetched either way.
        job = self.job
        os.makedirs(os.path.join(self.tmp, "mol.out"), exist_ok=True)

        paths = fetch_results(self.transport, job, self.tmp)

        self.assertNotIn(os.path.join(self.tmp, "mol.out"), paths)


class TestTwoInstancesShareTheJobList(unittest.TestCase):
    """Each holds the whole list; the second to save used to erase the first."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="store_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def store(self) -> JobStore:
        return JobStore(directory=self.tmp)

    def ids_on_disk(self) -> set:
        with open(os.path.join(self.tmp, "jobs.pmejbs"), encoding="utf-8") as handle:
            return {raw["id"] for raw in json.load(handle)["jobs"]}

    def test_a_second_window_does_not_erase_the_first_windows_jobs(self):
        first = self.store()
        first.add_job(make_job(id="from_first"))

        second = self.store()  # reads what exists
        third = self.store()
        first.add_job(make_job(id="also_first"))
        second.add_job(make_job(id="from_second"))

        self.assertEqual(self.ids_on_disk(), {"from_first", "also_first", "from_second"})
        self.assertIsNotNone(third)

    def test_our_own_view_wins_where_both_know_a_job(self):
        # Never adopt a stale record over one this session just observed.
        first = self.store()
        first.add_job(make_job(id="shared", state="RUNNING"))
        second = self.store()

        second.jobs["shared"].touch("DONE")
        second.save_jobs()

        with open(os.path.join(self.tmp, "jobs.pmejbs"), encoding="utf-8") as handle:
            states = {raw["id"]: raw["state"] for raw in json.load(handle)["jobs"]}
        self.assertEqual(states["shared"], "DONE")

    def test_a_removed_job_stays_removed(self):
        # Keeping unknown jobs must not undo a deliberate removal on the very
        # next save, which reads them straight back off disk.
        store = self.store()
        store.add_job(make_job(id="doomed"))
        store.add_job(make_job(id="kept"))

        store.remove_job("doomed")
        store.save_jobs()

        self.assertEqual(self.ids_on_disk(), {"kept"})

    def test_clearing_the_list_really_clears_it(self):
        store = self.store()
        store.add_job(make_job(id="a"))
        store.add_job(make_job(id="b"))

        store.clear_jobs()
        store.save_jobs()

        self.assertEqual(self.ids_on_disk(), set())

    def test_a_pruned_job_does_not_come_back(self):
        store = self.store()
        store.add_job(make_job(id="ancient", state="DONE", updated_at=1.0, finished_at=1.0))
        store.prefs["prune_days"] = 1

        store.prune()
        store.save_jobs()

        self.assertEqual(self.ids_on_disk(), set())

    def test_an_unreadable_file_does_not_stop_the_save(self):
        # read_json already tolerates a corrupt file; the merge must not turn
        # that into a failure to write at all.
        store = self.store()
        store.add_job(make_job(id="mine"))
        with open(os.path.join(self.tmp, "jobs.pmejbs"), "w", encoding="utf-8") as handle:
            handle.write("{ not json")

        store.save_jobs()

        self.assertEqual(self.ids_on_disk(), {"mine"})


class TestFetchStillRefusesTheInput(unittest.TestCase):
    """The staging rename must not have reopened the overwrite it guards."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="protect_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_input_file_is_never_written_over(self):
        source = os.path.join(self.tmp, "mol.xyz")
        with open(source, "w") as handle:
            handle.write("the user's own file\n")
        job = make_job(input_files=[source], fetch_globs=[])
        transport = FakeTransport()
        transport.when("ls -p -1", stdout="mol.xyz\n")

        try:
            fetch_results(transport, job, self.tmp)
        except TransportError:  # pragma: no cover - would be the bug
            self.fail("the guard raised instead of skipping")

        with open(source) as handle:
            self.assertEqual(handle.read(), "the user's own file\n")


if __name__ == "__main__":
    unittest.main()
