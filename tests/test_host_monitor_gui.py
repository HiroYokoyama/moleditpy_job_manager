"""The live host panel: what it draws, what it asks, and when it stops.

The costly part of this feature is that it contacts hosts on a timer, so most
of what matters is about *not* doing that: not while the window is closed, not
twice over for a host that has not answered yet, and not with a fresh
connection every couple of seconds.
"""

from __future__ import annotations

import unittest

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import SCHEDULER_WINDOWS, Job  # noqa: E402

from .fakes import make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402

SAMPLE = "cores=8\nload=1.60 1.20 0.90\nmem_total=64000\nmem_free=16000\n"


class CountingTransport:
    """Answers the probe, and counts how often it was used and closed."""

    def __init__(self, output: str = SAMPLE, fail: str = "") -> None:
        self.output = output
        self.fail = fail
        self.runs = 0
        self.closes = 0
        self.commands: list = []

    def run(self, command, timeout=None):
        self.runs += 1
        self.commands.append(command)
        if self.fail:
            raise RuntimeError(self.fail)

        class Result:
            stdout = self.output
            stderr = ""
            rc = 0

        return Result()

    def close(self):
        self.closes += 1


class HostMonitorTestCase(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.transports = {}
        self.service.transport_for = self._transport_for

    def _transport_for(self, host):
        return self.transports.setdefault(host.id, CountingTransport())

    def monitor(self):
        from job_manager.host_monitor import HostMonitorDialog

        dialog = HostMonitorDialog(self.service, None)
        self.addCleanup(dialog.deleteLater)
        return dialog


class TestWhatItShows(HostMonitorTestCase):
    def test_a_card_for_every_host(self):
        self.store.add_host(make_host(id="second", name="workstation"))
        dialog = self.monitor()
        self.assertEqual(len(dialog.cards), 2)

    def test_the_first_sample_arrives_without_waiting_for_the_timer(self):
        # Opening the window and looking at an empty card for two seconds is a
        # worse first impression than one command's delay.
        dialog = self.monitor()
        self.assertIn("load", dialog.cards[self.host.id].lbl_summary.text())

    def test_the_graph_grows_a_point_per_sample(self):
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        before = len(card.graph_load.values)

        dialog._sample_all()
        dialog._sample_all()

        self.assertEqual(len(card.graph_load.values), before + 2)

    def test_the_graph_keeps_a_bounded_history(self):
        from job_manager.host_monitor import HISTORY

        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        for _ in range(HISTORY + 20):
            dialog._sample_all()
        self.assertEqual(len(card.graph_load.values), HISTORY)

    def test_an_unreachable_host_says_so_and_keeps_the_others(self):
        self.store.add_host(make_host(id="second", name="down"))
        self.transports["second"] = CountingTransport(fail="ssh: connect: timed out")
        dialog = self.monitor()

        self.assertIn("timed out", dialog.cards["second"].lbl_summary.text())
        self.assertIn("load", dialog.cards[self.host.id].lbl_summary.text())

    def test_a_windows_host_is_asked_in_powershell(self):
        self.store.hosts.clear()
        self.store.add_host(make_host(id="win", name="windows box", scheduler=SCHEDULER_WINDOWS))
        self.monitor()
        self.assertIn("Get-CimInstance", self.transports["win"].commands[0])


class TestWhatItCosts(HostMonitorTestCase):
    def test_one_connection_is_reused_across_samples(self):
        # At a two-second cadence, reconnecting each time would cost more than
        # the measurement.
        dialog = self.monitor()
        for _ in range(5):
            dialog._sample_all()
        transport = self.transports[self.host.id]
        self.assertGreaterEqual(transport.runs, 6)
        self.assertEqual(transport.closes, 0)

    def test_closing_stops_the_timer_and_hands_the_connection_back(self):
        dialog = self.monitor()

        dialog.reject()

        self.assertFalse(dialog._timer.isActive())
        self.assertEqual(self.transports[self.host.id].closes, 1)

    def test_a_host_still_answering_is_not_asked_again(self):
        # A host slower than the interval would otherwise get one worker per
        # tick until the pool is full of them, making it slower still.
        dialog = self.monitor()
        transport = self.transports[self.host.id]
        before = transport.runs
        dialog._busy.add(self.host.id)

        dialog._sample_all()

        self.assertEqual(transport.runs, before)

    def test_a_failed_probe_drops_the_connection(self):
        # The next tick builds a new one rather than reusing a socket the far
        # end has already closed.
        self.transports[self.host.id] = CountingTransport(fail="broken pipe")
        self.monitor()
        self.assertEqual(self.transports[self.host.id].closes, 1)

    def test_a_host_that_would_prompt_for_a_password_is_left_alone(self):
        from job_manager.models import BACKEND_PARAMIKO

        self.store.hosts.clear()
        self.store.add_host(
            make_host(id="ask", name="asks", backend=BACKEND_PARAMIKO, ask_password=True)
        )
        dialog = self.monitor()

        dialog._sample_all()

        self.assertNotIn("ask", self.transports)

    def test_the_interval_is_two_seconds_and_adjustable(self):
        dialog = self.monitor()
        self.assertEqual(dialog.spin_interval.value(), 2)
        dialog.spin_interval.setValue(30)
        self.assertEqual(dialog._timer.interval(), 30000)


class TestTheButtonOnTheMonitor(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_one_window_at_a_time(self):
        self.dialog.open_host_monitor()
        first = self.dialog._host_monitor
        self.addCleanup(first.deleteLater)

        self.dialog.open_host_monitor()

        self.assertIs(self.dialog._host_monitor, first)

    def test_closing_it_lets_the_next_one_be_built(self):
        self.dialog.open_host_monitor()
        opened = self.dialog._host_monitor
        opened.reject()
        self.assertIsNone(self.dialog._host_monitor)


class TestElapsedTicks(DialogTestCase):
    """Elapsed used to advance in poll-sized jumps, two minutes by default."""

    def setUp(self):
        super().setUp()
        self.store.add_job(
            Job(id="j1", name="opt", host_id=self.host.id, state="RUNNING", submitted_at=1.0)
        )
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.seen = []
        self.dialog.model.dataChanged.connect(
            lambda tl, br, roles=None: self.seen.append((tl.row(), tl.column()))
        )

    def test_it_repaints_only_the_elapsed_column(self):
        from job_manager.jobs_dialog import COLUMNS

        self.dialog.show()
        self.dialog._tick_elapsed()
        self.assertEqual(self.seen, [(0, COLUMNS.index("Elapsed"))])

    def test_a_finished_job_is_left_alone(self):
        self.store.jobs["j1"].touch("DONE")
        self.dialog.show()

        self.dialog._tick_elapsed()

        self.assertEqual(self.seen, [])

    def test_a_hidden_window_paints_nothing(self):
        self.dialog.hide()
        self.dialog._tick_elapsed()
        self.assertEqual(self.seen, [])

    def test_the_timer_runs_once_a_second(self):
        self.assertTrue(self.dialog._ticker.isActive())
        self.assertEqual(self.dialog._ticker.interval(), 1000)


if __name__ == "__main__":
    unittest.main()


class TestTheRowMenu(DialogTestCase):
    """Right click offers the same actions as the buttons, disabled alike."""

    def setUp(self):
        super().setUp()
        self.store.add_job(
            Job(
                id="j1",
                name="opt",
                host_id=self.host.id,
                state="RUNNING",
                remote_dir="~/work/opt",
                log_file="job.log",
            )
        )
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.table.selectRow(0)

    def menu_entries(self):
        from unittest.mock import patch

        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu

        captured = {}
        original = QMenu.exec

        def capture(menu, *args, **kwargs):
            captured["menu"] = menu
            return None

        with patch.object(QMenu, "exec", capture):
            self.dialog._show_row_menu(QPoint(1, 1))
        QMenu.exec = original
        menu = captured.get("menu")
        return [] if menu is None else [a for a in menu.actions() if not a.isSeparator()]

    def test_every_button_is_offered(self):
        labels = [action.text() for action in self.menu_entries()]
        for expected in ("Tail Log", "Details", "Download", "Resubmit", "Remove"):
            self.assertIn(expected, labels)

    def test_an_action_that_cannot_run_is_disabled(self):
        # Open Result needs downloaded files, which this job has none of.
        entries = {action.text(): action for action in self.menu_entries()}
        self.assertFalse(entries["Open Result"].isEnabled())
        self.assertTrue(entries["Tail Log"].isEnabled())

    def test_it_matches_the_buttons_exactly(self):
        # Driven from the buttons, so the two cannot disagree about what is
        # possible for a job.
        entries = {action.text(): action.isEnabled() for action in self.menu_entries()}
        for button in (self.dialog.btn_tail, self.dialog.btn_download, self.dialog.btn_remove):
            self.assertEqual(entries[button.text()], button.isEnabled(), button.text())

    def test_no_menu_without_a_job(self):
        self.store.remove_job("j1")
        self.dialog.model.reload()
        self.assertEqual(self.menu_entries(), [])


class TestChoosingWhatToDownload(DialogTestCase):
    """Download lists the job directory rather than fetching in silence."""

    def setUp(self):
        super().setUp()
        self.job = Job(
            id="j1",
            name="opt",
            host_id=self.host.id,
            state="DONE",
            rc=0,
            remote_dir="~/work/opt",
            log_file="job.log",
            fetch_globs=["*.out"],
        )
        self.store.add_job(self.job)
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.table.selectRow(0)

    def chooser(self, listing="mol.out\nmol.gbw\njob.log\nsub/\n"):
        from unittest.mock import patch

        from job_manager.download_dialog import DownloadDialog

        # The chooser lists the tree, not one directory: it wants to show the
        # scratch folder nobody wrote a pattern for.
        self.transport.when("find", stdout=listing)
        captured = {}
        with patch.object(
            DownloadDialog, "exec", lambda self: captured.setdefault("d", self) and 0
        ):
            self.dialog._download_selected()
        return captured.get("d")

    def listed(self, dialog):
        """Every file in the tree, by its path relative to the job directory."""
        from job_manager.download_dialog import PATH_ROLE

        return [item.data(0, PATH_ROLE) for item in dialog._leaves()]

    def test_it_lists_the_job_directory(self):
        dialog = self.chooser()
        self.assertIsNotNone(dialog)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("mol.out", self.listed(dialog))
        self.assertIn("mol.gbw", self.listed(dialog))

    def test_the_wrappers_log_is_offered_but_not_ticked(self):
        # Never fetched by a pattern, always available to take deliberately.
        dialog = self.chooser()
        self.addCleanup(dialog.deleteLater)
        self.assertIn("job.log", self.listed(dialog))
        self.assertNotIn("job.log", dialog.chosen())

    def test_a_sub_directory_is_a_branch(self):
        dialog = self.chooser(listing="mol.out\nscratch/tmp.xyz\nscratch/deep/a.out\n")
        self.addCleanup(dialog.deleteLater)
        self.assertIn("scratch/deep/a.out", self.listed(dialog))
        top = [dialog.tree.topLevelItem(i).text(0) for i in range(dialog.tree.topLevelItemCount())]
        self.assertIn("scratch", top)

    def test_ticking_a_folder_takes_what_is_under_it(self):
        dialog = self.chooser(listing="mol.out\nscratch/tmp.xyz\nscratch/deep/a.out\n")
        self.addCleanup(dialog.deleteLater)
        from PyQt6.QtCore import Qt

        folder = [
            dialog.tree.topLevelItem(i)
            for i in range(dialog.tree.topLevelItemCount())
            if dialog.tree.topLevelItem(i).text(0) == "scratch"
        ][0]
        folder.setCheckState(0, Qt.CheckState.Checked)

        self.assertIn("scratch/deep/a.out", dialog.chosen())
        self.assertIn("scratch/tmp.xyz", dialog.chosen())

    def test_what_matched_is_ticked_and_the_rest_is_not(self):
        dialog = self.chooser()
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.chosen(), ["mol.out"])

    def test_nothing_matching_says_so_and_still_lists(self):
        # The case the dialog exists for: a finished job that appears to have
        # produced nothing because the patterns were wrong.
        self.job.fetch_globs = ["*.nothing"]
        dialog = self.chooser()
        self.addCleanup(dialog.deleteLater)

        self.assertIn("Nothing matched", dialog.lbl_headline.text())
        self.assertEqual(dialog.chosen(), [])
        self.assertEqual(len(self.listed(dialog)), 3)

    def test_an_empty_directory_says_that_instead(self):
        dialog = self.chooser(listing="")
        self.addCleanup(dialog.deleteLater)
        self.assertIn("empty", dialog.lbl_headline.text())

    def test_nothing_is_fetched_until_the_dialog_is_accepted(self):
        # Pressing Download opens the chooser; it does not start a transfer.
        self.chooser()
        self.assertFalse(self.job.downloaded)

    def test_ticking_more_and_accepting_fetches_exactly_those(self):
        from unittest.mock import patch

        from job_manager.download_dialog import DownloadDialog

        self.transport.when("ls -p", stdout="mol.out\nmol.gbw\n")
        with (
            patch.object(DownloadDialog, "exec", return_value=1),
            patch.object(DownloadDialog, "chosen", return_value=["mol.out", "mol.gbw"]),
            patch.object(DownloadDialog, "folder", return_value=self.tmp),
            patch.object(self.service, "download") as download,
        ):
            self.dialog._download_selected()

        download.assert_called_once()
        self.assertEqual(download.call_args.kwargs["names"], ["mol.out", "mol.gbw"])
        self.assertEqual(download.call_args.kwargs["into"], self.tmp)

    def test_the_tick_buttons_work_on_a_selection(self):
        from PyQt6.QtCore import Qt

        dialog = self.chooser()
        self.addCleanup(dialog.deleteLater)
        item = dialog.tree.topLevelItem(1)
        item.setSelected(True)

        dialog._set_selected(Qt.CheckState.Checked)

        self.assertIn(item.text(0), dialog.chosen())

    def test_download_is_refused_while_nothing_is_ticked(self):
        self.job.fetch_globs = ["*.nothing"]
        dialog = self.chooser()
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.btn_download.isEnabled())
