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

from PyQt6.QtCore import Qt  # noqa: E402
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
        card = dialog.cards[self.host.id]
        self.assertEqual(card.meter_load.detail, "20%")
        self.assertIn("1.60 of 8 threads", card.meter_load.toolTip())
        self.assertAlmostEqual(card.meter_load.fraction, 0.2)

    def test_the_bars_are_what_is_shown_by_default(self):
        # The question people open this for is "is there room on that
        # machine?", which a bar answers from across the room.
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertTrue(card.meter_load.isVisibleTo(card))
        self.assertFalse(card.graph_load.isVisibleTo(card))
        self.assertFalse(card.expanded)

    def test_the_history_button_opens_every_card_at_once(self):
        self.store.add_host(make_host(id="second", name="workstation"))
        dialog = self.monitor()

        dialog.btn_history.setChecked(True)

        for card in dialog.cards.values():
            self.assertTrue(card.expanded, card.host.name)

    def test_pressing_it_again_closes_them(self):
        dialog = self.monitor()
        dialog.btn_history.setChecked(True)
        dialog.btn_history.setChecked(False)
        self.assertFalse(dialog.cards[self.host.id].expanded)

    def test_the_history_choice_is_remembered(self):
        dialog = self.monitor()
        dialog.btn_history.setChecked(True)

        self.assertTrue(self.store.get_pref("host_monitor_history", False))
        again = self.monitor()
        self.assertTrue(again.btn_history.isChecked())
        self.assertTrue(again.cards[self.host.id].expanded)

    def test_the_history_is_collected_even_while_it_is_hidden(self):
        # Expanding a card must not start the graph from nothing.
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        dialog._sample_all()
        dialog._sample_all()
        self.assertGreaterEqual(len(card.graph_load.values), 3)

    def test_memory_the_host_cannot_report_is_not_drawn_as_zero(self):
        self.transports[self.host.id] = CountingTransport(output="cores=4\nmem_total=16000\n")
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertIn("usage not reported", card.meter_memory.toolTip())
        self.assertEqual(card.meter_memory.fraction, 0.0)

    def test_the_bar_and_its_graph_share_a_colour(self):
        # Load green, memory blue, in both places: the pair read as one
        # reading rather than as two unrelated ones.
        from job_manager.host_monitor import GRAPH_LOAD, GRAPH_MEMORY

        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertEqual(card.meter_load.color.name(), GRAPH_LOAD.name())
        self.assertEqual(card.graph_load.color.name(), GRAPH_LOAD.name())
        self.assertEqual(card.meter_memory.color.name(), GRAPH_MEMORY.name())
        self.assertEqual(card.graph_memory.color.name(), GRAPH_MEMORY.name())

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

        self.assertIn("timed out", dialog.cards["second"].lbl_state.text())
        self.assertEqual(dialog.cards[self.host.id].meter_load.detail, "20%")

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

    def test_closing_does_not_block_the_caller_on_the_network(self):
        # transport.close() (paramiko especially) can block sending a
        # disconnect over a stalled socket. Closing the window queued it onto
        # the pool instead of calling it inline, so a pool that never runs its
        # queued work must still return from reject()/closeEvent() immediately,
        # with the transport already forgotten either way.
        class QueueOnlyPool:
            def __init__(self):
                self.queued = []

            def start(self, task):
                self.queued.append(task)

            def clear(self):
                pass

            def waitForDone(self, msecs):
                pass

        dialog = self.monitor()
        pool = QueueOnlyPool()
        dialog.service.pool = pool
        transport = self.transports[self.host.id]

        dialog.reject()

        self.assertEqual(transport.closes, 0)  # not run inline
        self.assertEqual(len(pool.queued), 1)  # queued for the pool instead
        self.assertEqual(dialog._transports, {})  # forgotten immediately regardless

    def test_a_host_that_would_prompt_for_a_password_is_left_alone(self):
        from job_manager.models import BACKEND_PARAMIKO

        self.store.hosts.clear()
        self.store.add_host(
            make_host(id="ask", name="asks", backend=BACKEND_PARAMIKO, ask_password=True)
        )
        dialog = self.monitor()

        dialog._sample_all()

        self.assertNotIn("ask", self.transports)

    def test_openssh_is_asked_far_less_often_than_a_kept_connection(self):
        # OpenSSH spawns a whole ssh process per command -- on Windows it
        # cannot multiplex -- so a two-second cadence is a fresh connect,
        # handshake and authentication every two seconds. A burst of those
        # trips sshd's own throttling, which arrives here as a timeout on a
        # perfectly healthy host.
        from job_manager.host_monitor import DEFAULT_INTERVAL_SECONDS, OPENSSH_INTERVAL_SECONDS

        dialog = self.monitor()
        self.assertEqual(dialog.spin_interval.value(), OPENSSH_INTERVAL_SECONDS)

        from job_manager.models import BACKEND_LOCAL

        self.store.hosts.clear()
        self.store.add_host(make_host(id="here", name="this machine", backend=BACKEND_LOCAL))
        self.assertEqual(self.monitor().spin_interval.value(), DEFAULT_INTERVAL_SECONDS)

    def test_the_interval_is_adjustable(self):
        dialog = self.monitor()
        dialog.spin_interval.setValue(30)
        self.assertEqual(dialog._timer.interval(), 30000)

    def test_a_host_that_failed_is_asked_less_often(self):
        self.transports[self.host.id] = CountingTransport(fail="timed out")
        dialog = self.monitor()
        after_first = self.transports[self.host.id].runs

        dialog._sample_all()

        # Skipped rather than retried: asking an unreachable host every tick
        # for as long as the window is open is a denial of service against
        # one's own cluster.
        self.assertEqual(self.transports[self.host.id].runs, after_first)
        self.assertIn("retrying in", dialog.cards[self.host.id].lbl_state.text())

    def test_the_wait_doubles_while_it_keeps_failing(self):
        self.transports[self.host.id] = CountingTransport(fail="timed out")
        dialog = self.monitor()
        first = dialog._backoff[self.host.id]

        for _ in range(20):
            dialog._sample_all()

        self.assertGreater(dialog._backoff[self.host.id], first)

    def test_a_host_that_comes_back_is_asked_normally_again(self):
        transport = CountingTransport(fail="timed out")
        self.transports[self.host.id] = transport
        dialog = self.monitor()
        transport.fail = ""

        for _ in range(5):
            dialog._sample_all()

        self.assertNotIn(self.host.id, dialog._backoff)
        self.assertNotIn(self.host.id, dialog._skip_ticks)


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


class TestOpeningAJobList(DialogTestCase):
    """The counterparts to Save As..., at the head of the list row."""

    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)

    def test_both_buttons_are_there(self):
        self.assertEqual(self.dialog.btn_open_default.text(), "Default")
        self.assertEqual(self.dialog.btn_open_list.text(), "Open...")

    def test_open_goes_back_to_the_plugins_own_list(self):
        import os

        other = os.path.join(self.tmp, "other.pmejbs")
        self.store.use_jobs_file(other)
        self.assertNotEqual(self.store.jobs_path, self.store.default_jobs_path)

        self.dialog.btn_open_default.click()

        self.assertEqual(self.store.jobs_path, self.store.default_jobs_path)

    def test_open_dots_opens_the_file_that_was_chosen(self):
        import os
        from unittest.mock import patch

        from PyQt6.QtWidgets import QFileDialog

        path = os.path.join(self.tmp, "saved.pmejbs")
        self.dialog._export_to(path, ".pmejbs") if hasattr(self.dialog, "_export_to") else None
        with (
            patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")),
            patch.object(self.dialog, "open_job_list") as opened,
        ):
            self.dialog.btn_open_list.click()

        opened.assert_called_once_with(path)

    def test_cancelling_the_picker_opens_nothing(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QFileDialog

        with (
            patch.object(QFileDialog, "getOpenFileName", return_value=("", "")),
            patch.object(self.dialog, "open_job_list") as opened,
        ):
            self.dialog.btn_open_list.click()

        opened.assert_not_called()


class TestTheCardsStack(HostMonitorTestCase):
    """Cards go in a grid, as many columns as the window has room for."""

    def eight_hosts(self):
        for index in range(7):
            self.store.add_host(make_host(id=f"h{index}", name=f"host {index}"))

    def test_a_narrow_window_is_one_column(self):
        self.eight_hosts()
        dialog = self.monitor()
        dialog.resize(360, 600)
        dialog._relayout()

        positions = [dialog.grid.getItemPosition(i)[:2] for i in range(dialog.grid.count())]
        self.assertTrue(all(column == 0 for _row, column in positions), positions)

    def test_a_wide_window_uses_the_room(self):
        self.eight_hosts()
        dialog = self.monitor()
        dialog.resize(1400, 600)
        dialog._relayout()

        columns = {dialog.grid.getItemPosition(i)[1] for i in range(dialog.grid.count())}
        self.assertGreater(len(columns), 1)

    def test_every_card_is_placed_exactly_once(self):
        self.eight_hosts()
        dialog = self.monitor()
        dialog.resize(1000, 600)
        dialog._relayout()

        placed = [dialog.grid.itemAt(i).widget() for i in range(dialog.grid.count())]
        for card in dialog.cards.values():
            self.assertEqual(placed.count(card), 1, card.host.name)

    def test_relayout_is_skipped_when_the_column_count_is_unchanged(self):
        # resizeEvent fires on every pixel of a drag; rebuilding the grid each
        # time would fight the user's mouse.
        dialog = self.monitor()
        dialog.resize(1000, 600)
        dialog._relayout()
        before = dialog._laid_out_for

        dialog.resize(1010, 600)
        dialog._relayout()

        self.assertEqual(dialog._laid_out_for, before)


class TestTheCardResponds(HostMonitorTestCase):
    def test_dark_mode_is_off_by_default_and_remembered(self):
        dialog = self.monitor()
        self.assertFalse(dialog.btn_dark.isChecked())

        dialog.btn_dark.setChecked(True)

        self.assertTrue(self.store.get_pref("host_monitor_dark", False))
        self.assertTrue(self.monitor().btn_dark.isChecked())

    def test_dark_mode_changes_this_window_only(self):
        from PyQt6.QtGui import QPalette

        dialog = self.monitor()
        before = dialog.palette().color(QPalette.ColorRole.Window).name()
        card_before = dialog.cards[self.host.id].styleSheet()

        dialog.btn_dark.setChecked(True)

        self.assertNotEqual(dialog.palette().color(QPalette.ColorRole.Window).name(), before)
        # And the cards follow. They carry a style sheet, which resolves their
        # own palette -- so they are restyled from the window's palette rather
        # than left to read their own.
        self.assertNotEqual(dialog.cards[self.host.id].styleSheet(), card_before)

    def test_the_graphs_are_green_and_blue(self):
        from job_manager.host_monitor import GRAPH_LOAD, GRAPH_MEMORY

        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertEqual(card.graph_load.color.name(), GRAPH_LOAD.name())
        self.assertEqual(card.graph_memory.color.name(), GRAPH_MEMORY.name())


class TestTheDarkToggleGoesBothWays(HostMonitorTestCase):
    """Turning it off must give back the palette the window started with."""

    def palette_name(self, dialog):
        from PyQt6.QtGui import QPalette

        return dialog.palette().color(QPalette.ColorRole.Window).name()

    def test_off_restores_exactly_what_was_there(self):
        dialog = self.monitor()
        before = self.palette_name(dialog)

        dialog.btn_dark.setChecked(True)
        dialog.btn_dark.setChecked(False)

        self.assertEqual(self.palette_name(dialog), before)

    def test_every_role_comes_back_not_only_the_ones_dark_mode_set(self):
        # Building the dark palette from scratch left Light, Midlight, Dark
        # and Shadow at defaults, and the mixture came back muddy.
        from PyQt6.QtGui import QPalette

        dialog = self.monitor()
        roles = (
            QPalette.ColorRole.Window,
            QPalette.ColorRole.Base,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.Light,
            QPalette.ColorRole.Midlight,
            QPalette.ColorRole.Dark,
            QPalette.ColorRole.Shadow,
            QPalette.ColorRole.Button,
        )
        before = {role: dialog.palette().color(role).name() for role in roles}

        dialog.btn_dark.setChecked(True)
        dialog.btn_dark.setChecked(False)

        after = {role: dialog.palette().color(role).name() for role in roles}
        self.assertEqual(after, before)

    def test_the_card_style_comes_back_with_the_palette(self):
        # Turning the toggle off has to undo the card as exactly as it undoes
        # the window; a half-restored pair is what read as muddy brown.
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        before = card.styleSheet()

        dialog.btn_dark.setChecked(True)
        dialog.btn_dark.setChecked(False)

        self.assertEqual(card.styleSheet(), before)


class TestAFailedProbeIsNotAnAlarm(HostMonitorTestCase):
    """A host that did not answer is shown on its card, not in the log."""

    def test_it_is_logged_at_debug_not_warning(self):
        self.transports[self.host.id] = CountingTransport(fail="timed out after 15s")

        with self.assertLogs("root", level="DEBUG") as caught:
            self.monitor()

        levels = {record.levelname for record in caught.records if "background task" in record.msg}
        self.assertEqual(levels, {"DEBUG"})

    def test_other_work_still_warns(self):
        # The quiet flag is per call, not a global loosening.
        from job_manager.tasks import run_async

        class Pool:
            def start(self, task, *a, **k):
                task.run_sync()

        def boom():
            raise RuntimeError("something nobody expected")

        with self.assertLogs("root", level="WARNING") as caught:
            run_async(Pool(), boom, on_error=lambda _m: None)

        self.assertTrue(any("background task" in record.msg for record in caught.records))


class TestTheChosenIntervalSticks(HostMonitorTestCase):
    def test_a_setting_survives_reopening(self):
        dialog = self.monitor()
        dialog.spin_interval.setValue(2)

        again = self.monitor()

        self.assertEqual(again.spin_interval.value(), 2)
        self.assertEqual(again._timer.interval(), 2000)

    def test_the_backend_default_applies_only_until_then(self):
        from job_manager.host_monitor import OPENSSH_INTERVAL_SECONDS

        self.assertEqual(self.monitor().spin_interval.value(), OPENSSH_INTERVAL_SECONDS)
        self.assertEqual(self.store.get_pref("host_monitor_interval", 0), 0)


class TestItOpensWideEnoughToCompare(HostMonitorTestCase):
    def test_two_columns_out_of_the_box(self):
        # One column reads as a list of three things; two is where the panel
        # becomes something you compare machines across.
        for index in range(3):
            self.store.add_host(make_host(id=f"h{index}", name=f"host {index}"))
        dialog = self.monitor()

        self.assertGreaterEqual(dialog.width(), 2 * dialog.CARD_WIDTH)
        self.assertEqual(dialog._columns(), 2)


class TestCPUMeterAndSparklineRenaming(HostMonitorTestCase):
    """Verifies that the primary meter and sparkline are labeled CPU and alias load."""

    def test_meter_and_sparkline_labeled_cpu(self):
        from job_manager.host_monitor import HostCard

        card = HostCard(self.host)
        self.addCleanup(card.deleteLater)
        self.assertEqual(card.meter_cpu.caption, "CPU")
        self.assertEqual(card.graph_cpu.caption, "CPU")

    def test_load_aliases_point_to_cpu_widgets(self):
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertIs(card.meter_load, card.meter_cpu)
        self.assertIs(card.graph_load, card.graph_cpu)

    def test_graph_cpu_constant_exported(self):
        from job_manager.host_monitor import GRAPH_CPU, GRAPH_LOAD

        self.assertEqual(GRAPH_CPU.name(), GRAPH_LOAD.name())


class TestHostMonitorIndependentWindow(HostMonitorTestCase):
    """Host monitor behaves as a separate top-level independent window."""

    def test_open_host_monitor_creates_parentless_window(self):
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.open_host_monitor()
        monitor = dialog._host_monitor
        self.assertIsNotNone(monitor)
        self.addCleanup(monitor.deleteLater)
        self.assertIsNone(monitor.parent())
        self.assertTrue(bool(monitor.windowFlags() & Qt.WindowType.Window))
        self.assertFalse(monitor.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose))

    def test_reopen_host_monitor_raises_existing_window(self):
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.open_host_monitor()
        first_monitor = dialog._host_monitor
        self.addCleanup(first_monitor.deleteLater)
        dialog.open_host_monitor()
        self.assertIs(dialog._host_monitor, first_monitor)


class TestDisabledHosts(HostMonitorTestCase):
    """A disabled host still gets a card, but is skipped by the timer."""

    def _add_disabled_host(self):
        from .fakes import make_host

        host = make_host(id="disabled_one", name="offline", enabled=False)
        self.store.add_host(host)
        return host

    def test_a_disabled_host_is_skipped_by_sampling(self):
        self._add_disabled_host()
        dialog = self.monitor()
        ids = [host.id for host in dialog._hosts()]
        self.assertNotIn("disabled_one", ids)
        self.assertIn(self.host.id, ids)

    def test_a_disabled_host_still_gets_a_card(self):
        self._add_disabled_host()
        dialog = self.monitor()
        self.assertIn("disabled_one", dialog.cards)

    def test_a_disabled_host_card_is_dimmed(self):
        self._add_disabled_host()
        dialog = self.monitor()
        card = dialog.cards["disabled_one"]
        self.assertFalse(card.isEnabled())
        self.assertIsNotNone(card.graphicsEffect())

    def test_an_enabled_host_card_is_not_dimmed(self):
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertTrue(card.isEnabled())
        self.assertIsNone(card.graphicsEffect())


class TestThreadScaledMeter(HostMonitorTestCase):
    """The CPU meter reads thread usability, matching its own label."""

    def test_label_and_meter_agree_on_threads(self):
        self.transports[self.host.id] = CountingTransport(
            output="cores=8\nthreads=16\nload=8.0 8.0 8.0\nmem_total=64000\nmem_free=16000\n"
        )
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertIn("16 threads", card.lbl_state.text())
        self.assertAlmostEqual(card.meter_cpu.fraction, 0.5)
        self.assertIn("16 threads", card.meter_cpu.toolTip())
