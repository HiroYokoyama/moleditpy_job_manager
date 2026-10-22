"""The job table can be sorted by clicking a header and filtered by typing.

Both go through a QSortFilterProxyModel sitting in front of the plain
JobTableModel, which is what keeps the model itself the simple, source-row-
indexed thing every other test already assumes.
"""

from __future__ import annotations

import unittest

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtCore import Qt  # noqa: E402

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
            self.dialog.proxy.index(row, COLUMNS.index("Name")).data()
            for row in range(self.dialog.proxy.rowCount())
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
            self.dialog.proxy.index(row, COLUMNS.index("Name")).data()
            for row in range(self.dialog.proxy.rowCount())
        ]

    def test_ascending_by_name(self):
        self.dialog.proxy.sort(COLUMNS.index("Name"), Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.names_in_order(), ["aaa", "bbb", "ccc"])

    def test_descending_by_name(self):
        self.dialog.proxy.sort(COLUMNS.index("Name"), Qt.SortOrder.DescendingOrder)
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

    def test_sorting_by_submitted_uses_the_real_timestamp(self):
        self.dialog.proxy.sort(COLUMNS.index("Submitted"), Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.names_in_order(), ["aaa", "ccc", "bbb"])

    def test_default_sort_is_most_recently_submitted_first(self):
        # c=3, b=2, a=1 by submission; by update it would be b, c, a.
        self.assertEqual(self.names_in_order(), ["bbb", "ccc", "aaa"])
        self.assertEqual(self.dialog.proxy.sortColumn(), COLUMNS.index("No."))

    def numbers_in_order(self):
        return [
            self.dialog.proxy.index(row, COLUMNS.index("No.")).data()
            for row in range(self.dialog.proxy.rowCount())
        ]

    def test_numbers_follow_submission_order_and_survive_filtering(self):
        self.assertEqual(self.numbers_in_order(), [3, 2, 1])
        self.dialog.proxy.sort(COLUMNS.index("Name"), Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.numbers_in_order(), [1, 3, 2])
        self.dialog.txt_filter.setText("bbb")
        self.assertEqual(self.numbers_in_order(), [3])

    def test_number_sort_is_numeric_beyond_nine_and_matches_submitted(self):
        for number in range(4, 13):
            self.store.add_job(Job(id=f"j{number}", submitted_at=float(number)))
        self.dialog.model.reload()
        for order in (Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder):
            self.dialog.proxy.sort(COLUMNS.index("No."), order)
            numbers = self.numbers_in_order()
            self.assertEqual(
                numbers, sorted(numbers, reverse=order == Qt.SortOrder.DescendingOrder)
            )
            self.dialog.proxy.sort(COLUMNS.index("Submitted"), order)
            self.assertEqual(self.numbers_in_order(), numbers)

    def test_numbers_refresh_when_submission_time_changes(self):
        self.a.submitted_at = 4.0
        self.dialog.model.refresh_job(self.a.id)
        self.assertEqual(self.names_in_order(), ["aaa", "bbb", "ccc"])
        self.assertEqual(self.numbers_in_order(), [3, 2, 1])

    def test_archive_numbers_use_its_submission_times_with_deterministic_ties(self):
        archived = [
            Job(id="z", submitted_at=9),
            Job(id="a", submitted_at=2),
            Job(id="b", submitted_at=2),
        ]
        self.dialog.model.show_archive(archived)
        self.assertEqual(self.numbers_in_order(), [3, 2, 1])
        self.dialog.proxy.sort(COLUMNS.index("Submitted"), Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.numbers_in_order(), [3, 2, 1])
        self.dialog.model.show_archive(None)
        self.assertEqual(self.numbers_in_order(), [3, 2, 1])

    def test_text_sort_is_case_insensitive(self):
        self.a.name, self.b.name, self.c.name = "apple", "Zoo", "Banana"
        self.dialog.model.reload()
        self.dialog.proxy.sort(COLUMNS.index("Name"), Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.names_in_order(), ["apple", "Banana", "Zoo"])

    def test_queue_ids_sort_numerically_and_allow_scheduler_suffixes(self):
        self.a.remote_job_id, self.b.remote_job_id, self.c.remote_job_id = "100", "20", "10.server"
        self.dialog.model.reload()
        self.dialog.proxy.sort(COLUMNS.index("Queue ID"), Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.names_in_order(), ["ccc", "aaa", "bbb"])


if __name__ == "__main__":
    unittest.main()
