"""Tests for OutputFileSelectorDialog and temporary output file caching."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from job_manager.models import Job, new_id
from job_manager.output_file_dialog import (
    OutputFileSelectorDialog,
    describe_file_type,
    format_file_size,
    IS_REMOTE_ROLE,
)

app = QApplication.instance() or QApplication([])


class TestOutputFileHelpers(unittest.TestCase):
    def test_describe_file_type(self):
        self.assertIn("ORCA", describe_file_type("opt.out"))
        self.assertIn("Gaussian", describe_file_type("calc.log"))
        self.assertIn("XYZ", describe_file_type("geom.xyz"))
        self.assertIn("Checkpoint", describe_file_type("calc.fchk"))
        self.assertIn("JSON", describe_file_type("results.json"))
        self.assertIn("CSV", describe_file_type("summary.csv"))
        self.assertEqual(describe_file_type("unknown.dat123"), "DAT123 File")

    def test_format_file_size(self):
        self.assertEqual(format_file_size(500), "500 B")
        self.assertEqual(format_file_size(2048), "2.0 KB")
        self.assertEqual(format_file_size(3 * 1024 * 1024), "3.0 MB")


class TestOutputFileSelectorDialog(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = MagicMock()
        self.job = Job(
            id=new_id(),
            name="benzene_opt",
            host_id="h1",
            remote_dir="/scratch/user/benzene_opt",
            local_dir=self.temp_dir,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_local_files_populates_tree(self):
        file1 = os.path.join(self.temp_dir, "benzene.out")
        file2 = os.path.join(self.temp_dir, "benzene.xyz")
        with open(file1, "w") as f:
            f.write("ORCA calculation output")
        with open(file2, "w") as f:
            f.write("6\n\nC 0 0 0\n")

        self.job.downloaded_files = [file1, file2]

        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        item0 = dialog.tree.topLevelItem(0)
        item1 = dialog.tree.topLevelItem(1)
        names = {item0.text(0), item1.text(0)}
        self.assertEqual(names, {"benzene.out", "benzene.xyz"})
        # benzene.out is prioritized
        self.assertEqual(dialog.tree.currentItem().text(0), "benzene.out")

    def test_filter_hides_non_matching_files(self):
        file1 = os.path.join(self.temp_dir, "benzene.out")
        file2 = os.path.join(self.temp_dir, "benzene.xyz")
        with open(file1, "w") as f:
            f.write("output")
        with open(file2, "w") as f:
            f.write("xyz")

        self.job.downloaded_files = [file1, file2]
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        dialog.txt_filter.setText("xyz")
        visible = [
            dialog.tree.topLevelItem(i).text(0)
            for i in range(dialog.tree.topLevelItemCount())
            if not dialog.tree.topLevelItem(i).isHidden()
        ]
        self.assertEqual(visible, ["benzene.xyz"])

    def test_open_local_file_calls_callback(self):
        file1 = os.path.join(self.temp_dir, "benzene.out")
        with open(file1, "w") as f:
            f.write("output")

        self.job.downloaded_files = [file1]
        opened = []
        dialog = OutputFileSelectorDialog(
            self.service, self.job, on_open_callback=lambda p: opened.append(p)
        )
        self.addCleanup(dialog.deleteLater)

        dialog.btn_open.click()
        self.assertEqual(opened, [file1])

    def test_remote_files_are_listed_and_still_openable(self):
        # No local files exist
        self.job.downloaded_files = []
        self.job.local_dir = ""

        # Setup mock for service.list_remote_results
        def fake_list(job, on_ok, on_error):
            on_ok(["remote_calc.out", "remote_geom.xyz"])

        self.service.list_remote_results.side_effect = fake_list

        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        # Dialog should list the 2 remote files
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        current = dialog.tree.currentItem()
        self.assertIsNotNone(current)
        self.assertEqual(current.text(0), "remote_calc.out")
        self.assertTrue(current.data(0, IS_REMOTE_ROLE))

        # Open is enabled for a remote file too: clicking it now offers to
        # download and open rather than silently doing nothing, which is what
        # a disabled button with no explanation looked like before.
        self.assertTrue(dialog.btn_open.isEnabled())
