"""The job table can be sorted by clicking a header and filtered by typing.

Both go through a QSortFilterProxyModel sitting in front of the plain
JobTableModel, which is what keeps the model itself the simple, source-row-
indexed thing every other test already assumes.
"""

from __future__ import annotations

import unittest

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtCore import Qt  # noqa: E402

from job_manager.jobs_dialog import COLUMNS, JobsDialog  # noqa: E402
from job_manager.models import STATE_RUNNING, Job  # noqa: E402

from .test_dialogs import DialogTestCase  # noqa: E402


class TestFilteringTheTable(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(Job(id="a", name="benzene", host_name="alpha", submitted_at=1.0))
        self.store.add_job(Job(id="b", name="toluene", host_name="beta", submitted_at=2.0))
        self.store.add_job(Job(id="c", name="water", host_name="alpha", submitted_at=3.0))
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def visible_names(self):
        return {
            self.dialog.proxy.index(row, 0).data() for row in range(self.dialog.proxy.rowCount())
        }

    def test_everything_is_visible_with_no_filter(self):
        self.assertEqual(self.visible_names(), {"benzene", "toluene", "water"})

    def test_filtering_by_name(self):
        self.dialog.txt_filter.setText("benz")
        self.assertEqual(self.visible_names(), {"benzene"})

    def test_filtering_by_host(self):
        self.dialog.txt_filter.setText("beta")
        self.assertEqual(self.visible_names(), {"toluene"})

    def test_filtering_is_case_insensitive(self):
        self.dialog.txt_filter.setText("BENZ")
        self.assertEqual(self.visible_names(), {"benzene"})

    def test_clearing_the_filter_shows_everything_again(self):
        self.dialog.txt_filter.setText("benz")
        self.dialog.txt_filter.setText("")
        self.assertEqual(self.visible_names(), {"benzene", "toluene", "water"})

    def test_no_match_shows_nothing(self):
        self.dialog.txt_filter.setText("xenon")
        self.assertEqual(self.visible_names(), set())

    def test_a_filtered_out_job_cannot_be_the_selection(self):
        self.dialog.table.selectRow(0)
        self.dialog.txt_filter.setText("xenon")
        self.assertIsNone(self.dialog.selected_job())


class TestSortingTheTable(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.a = Job(id="a", name="aaa", host_name="h", submitted_at=1.0, updated_at=100.0)
        self.b = Job(id="b", name="ccc", host_name="h", submitted_at=2.0, updated_at=300.0)
        self.c = Job(id="c", name="bbb", host_name="h", submitted_at=3.0, updated_at=200.0)
        for job in (self.a, self.b, self.c):
            self.store.add_job(job)
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def names_in_order(self):
        return [
            self.dialog.proxy.index(row, 0).data() for row in range(self.dialog.proxy.rowCount())
        ]

    def test_ascending_by_name(self):
        self.dialog.proxy.sort(0, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.names_in_order(), ["aaa", "bbb", "ccc"])

    def test_descending_by_name(self):
        self.dialog.proxy.sort(0, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.names_in_order(), ["ccc", "bbb", "aaa"])

    def test_sorting_by_updated_uses_the_real_timestamp_not_its_text(self):
        column = COLUMNS.index("Updated")
        self.dialog.proxy.sort(column, Qt.SortOrder.AscendingOrder)
        # a=100, c=200, b=300 -- chronological, not the alphabetical order the
        # formatted "MM-DD HH:MM" strings would happen to fall in here.
        self.assertEqual(self.names_in_order(), ["aaa", "bbb", "ccc"])

    def test_sorting_by_elapsed_uses_seconds_not_the_formatted_text(self):
        long_running = Job(
            id="d",
            name="longrun",
            host_name="h",
            state=STATE_RUNNING,
            started_at=0.0,
            submitted_at=0.0,
            updated_at=0.0,
        )
        short_running = Job(
            id="e",
            name="shortrun",
            host_name="h",
            state=STATE_RUNNING,
            started_at=1_000_000_000.0,
            submitted_at=1_000_000_000.0,
            updated_at=1_000_000_000.0,
        )
        import time

        long_running.started_at = time.time() - 700  # "11m 40s"
        short_running.started_at = time.time() - 65  # "1m 05s"
        self.store.add_job(long_running)
        self.store.add_job(short_running)
        self.dialog.model.reload()

        column = COLUMNS.index("Elapsed")
        self.dialog.proxy.sort(column, Qt.SortOrder.AscendingOrder)
        names = self.names_in_order()
        # "1m 05s" sorts before "11m 40s" as text; by real seconds it is the
        # other way around, which is what this proves.
        self.assertLess(names.index("shortrun"), names.index("longrun"))

    def test_default_sort_is_most_recently_updated_first(self):
        self.assertEqual(self.names_in_order(), ["ccc", "bbb", "aaa"])


if __name__ == "__main__":
    unittest.main()
