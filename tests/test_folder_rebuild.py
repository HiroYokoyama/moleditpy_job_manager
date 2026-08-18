"""Rebuilding a job list from a folder of results, and what it may not do.

The records this makes describe calculations nobody submitted from here, so the
flag that says so travels in the file and every action that would talk to a
host is refused while such a list is in use.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from job_manager.folder_scan import scan_folder, summarise
from job_manager.models import SENTINEL_NAME, STATE_DONE, STATE_FAILED
from job_manager.store import JobStore


def write(path: str, text: str = "x\n") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rebuild_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)


class TestWhatCountsAsACalculation(ScanCase):
    def test_a_directory_with_an_output_becomes_one_job(self):
        write(self.path("run1", "mol.out"))
        result = scan_folder(self.root)
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].local_dir, self.path("run1"))

    def test_a_directory_with_no_output_is_not_a_job(self):
        write(self.path("notes", "readme.txt"))
        self.assertEqual(len(scan_folder(self.root).jobs), 0)

    def test_each_directory_is_its_own_job(self):
        write(self.path("a", "mol.out"))
        write(self.path("b", "mol.out"))
        self.assertEqual(len(scan_folder(self.root).jobs), 2)

    def test_the_input_file_names_the_job(self):
        write(self.path("run1", "benzene.inp"))
        write(self.path("run1", "benzene.out"))
        job = scan_folder(self.root).jobs[0]
        self.assertEqual(job.name, "benzene")
        self.assertTrue(job.input_files)

    def test_without_an_input_the_directory_names_it(self):
        write(self.path("water_sp", "mol.out"))
        self.assertEqual(scan_folder(self.root).jobs[0].name, "water_sp")

    def test_the_outputs_are_recorded_as_downloaded(self):
        write(self.path("run1", "mol.out"))
        write(self.path("run1", "mol.xyz"))
        job = scan_folder(self.root).jobs[0]
        self.assertTrue(job.downloaded)
        self.assertEqual(len(job.downloaded_files), 2)

    def test_a_sentinel_left_by_this_plugin_gives_the_real_outcome(self):
        write(self.path("run1", "mol.out"))
        write(self.path("run1", SENTINEL_NAME), "1\n")
        job = scan_folder(self.root).jobs[0]
        self.assertEqual(job.state, STATE_FAILED)
        self.assertEqual(job.rc, 1)

    def test_without_a_sentinel_it_is_taken_as_finished(self):
        write(self.path("run1", "mol.out"))
        self.assertEqual(scan_folder(self.root).jobs[0].state, STATE_DONE)

    def test_a_zero_sentinel_is_done(self):
        write(self.path("run1", "mol.out"))
        write(self.path("run1", SENTINEL_NAME), "0\n")
        self.assertEqual(scan_folder(self.root).jobs[0].state, STATE_DONE)

    def test_noise_directories_are_never_walked(self):
        write(self.path(".git", "objects", "mol.out"))
        write(self.path("__pycache__", "mol.out"))
        self.assertEqual(len(scan_folder(self.root).jobs), 0)

    def test_depth_is_limited(self):
        deep = self.path(*[f"level{n}" for n in range(9)])
        write(os.path.join(deep, "mol.out"))
        self.assertEqual(len(scan_folder(self.root, max_depth=3).jobs), 0)

    def test_a_huge_folder_is_truncated_rather_than_walked_for_ever(self):
        for index in range(30):
            write(self.path(f"run{index}", "mol.out"))
        result = scan_folder(self.root, max_files=5)
        self.assertTrue(result.truncated)

    def test_newest_first(self):
        import time

        write(self.path("older", "mol.out"))
        time.sleep(0.02)
        write(self.path("newer", "mol.out"))
        os.utime(self.path("newer", "mol.out"), (time.time(), time.time() + 100))
        self.assertEqual(scan_folder(self.root).jobs[0].name, "newer")

    def test_a_missing_folder_finds_nothing(self):
        self.assertEqual(len(scan_folder(os.path.join(self.root, "nope")).jobs), 0)

    def test_the_summary_counts_files_and_failures(self):
        write(self.path("run1", "mol.out"))
        write(self.path("run1", SENTINEL_NAME), "2\n")
        write(self.path("run2", "mol.out"))
        counts = summarise(scan_folder(self.root))
        self.assertEqual(counts["jobs"], 2)
        self.assertEqual(counts["files"], 2)
        self.assertEqual(counts["failed"], 1)


class TestTheFlagTravelsInTheFile(ScanCase):
    """A rebuilt list stays read only after it is closed, moved or reopened."""

    def setUp(self):
        super().setUp()
        self.data = tempfile.mkdtemp(prefix="rebuild_store_")
        self.addCleanup(shutil.rmtree, self.data, ignore_errors=True)
        self.store = JobStore(directory=self.data)

    def written_list(self) -> str:
        write(self.path("run1", "mol.out"))
        jobs = scan_folder(self.root).jobs
        path = os.path.join(self.root, "rebuilt.pmejbs")
        return self.store.write_job_list(path, jobs, reconstructed=True)

    def test_the_file_says_it_was_reconstructed(self):
        path = self.written_list()
        self.assertTrue(self.store.read_job_flags(path)["reconstructed"])

    def test_opening_it_marks_the_store(self):
        path = self.written_list()
        self.store.use_jobs_file(path)
        self.assertTrue(self.store.reconstructed)

    def test_going_back_to_the_default_list_clears_it(self):
        path = self.written_list()
        self.store.use_jobs_file(path)
        self.store.use_jobs_file("")
        self.assertFalse(self.store.reconstructed)

    def test_saving_keeps_the_flag(self):
        path = self.written_list()
        self.store.use_jobs_file(path)
        self.store.save_jobs()
        self.assertTrue(self.store.read_job_flags(path)["reconstructed"])

    def test_an_ordinary_list_is_not_reconstructed(self):
        path = os.path.join(self.root, "plain.pmejbs")
        self.store.write_job_list(path, [])
        self.assertFalse(self.store.read_job_flags(path)["reconstructed"])


if __name__ == "__main__":
    unittest.main()
