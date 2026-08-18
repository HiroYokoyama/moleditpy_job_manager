"""Tests for the theme module and its wiring into every dialog.

All tests that touch Qt widgets are guarded by the same importorskip as the
rest of the GUI suite, so this file is safe on a bare pytest-only install.
"""

from __future__ import annotations

import unittest

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtGui import QPalette, QColor  # noqa: E402

from job_manager import theme  # noqa: E402
from job_manager.host_monitor import BLANK, HOURGLASS, _ActiveJobsBar  # noqa: E402
from job_manager.hosts_dialog import HostsDialog  # noqa: E402
from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import STATE_RUNNING, STATE_FAILED, STATE_PENDING, Job  # noqa: E402
from job_manager.status_widget import JobStatusWidget  # noqa: E402
from job_manager.text_dialog import TextDialog  # noqa: E402
from job_manager.details_dialog import JobDetailsDialog  # noqa: E402
from job_manager.download_dialog import DownloadDialog  # noqa: E402

from .test_dialogs import DialogTestCase  # noqa: E402
from .test_host_monitor_gui import HostMonitorTestCase  # noqa: E402


# ---------------------------------------------------------------------------
# theme.py — constants and stylesheet content
# ---------------------------------------------------------------------------


class TestThemeConstants(unittest.TestCase):
    """The token values that the rest of the plugin is compiled against."""

    def test_accent_is_a_valid_hex_colour(self):
        self.assertTrue(QColor(theme.CY_ACCENT).isValid())

    def test_accent2_is_a_valid_hex_colour(self):
        self.assertTrue(QColor(theme.CY_ACCENT2).isValid())

    def test_all_state_colours_are_valid(self):
        for attr in ("CY_GREEN", "CY_RED", "CY_AMBER", "CY_TEAL", "CY_PURPLE", "CY_GREY"):
            color = QColor(getattr(theme, attr))
            self.assertTrue(color.isValid(), f"{attr} is not a valid colour")

    def test_stylesheet_mentions_QPushButton(self):
        self.assertIn("QPushButton", theme.DIALOG_STYLESHEET)

    def test_stylesheet_mentions_QTableView(self):
        self.assertIn("QTableView", theme.DIALOG_STYLESHEET)

    def test_stylesheet_mentions_scrollbar(self):
        self.assertIn("QScrollBar", theme.DIALOG_STYLESHEET)

    def test_stylesheet_mentions_accent(self):
        # The accent colour must actually appear in the sheet, not just be
        # defined as a constant nobody uses.
        self.assertIn(theme.CY_ACCENT, theme.DIALOG_STYLESHEET)

    def test_all_expected_names_are_exported(self):
        expected = {
            "CY_GREEN",
            "CY_RED",
            "CY_AMBER",
            "CY_TEAL",
            "CY_PURPLE",
            "CY_GREY",
            "CY_ACCENT",
            "CY_ACCENT2",
            "DIALOG_STYLESHEET",
        }
        self.assertTrue(expected.issubset(set(theme.__all__)))


# ---------------------------------------------------------------------------
# Dialog stylesheet application
# ---------------------------------------------------------------------------


class TestDialogStylesheets(DialogTestCase):
    """Every dialog must carry the shared stylesheet on its root widget."""

    def _has_stylesheet(self, dialog):
        """True if the dialog's own stylesheet contains at least QPushButton."""
        return "QPushButton" in dialog.styleSheet()

    def test_jobs_dialog_carries_the_stylesheet(self):
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(self._has_stylesheet(dialog))

    def test_hosts_dialog_carries_the_stylesheet(self):
        dialog = HostsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(self._has_stylesheet(dialog))

    def test_text_dialog_carries_the_stylesheet(self):
        dialog = TextDialog("test", "hello")
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(self._has_stylesheet(dialog))

    def test_details_dialog_carries_the_stylesheet(self):
        job = Job(id="j1", name="mol", submitted_at=1000.0)
        dialog = JobDetailsDialog(self.service, job, "record text", "Details")
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(self._has_stylesheet(dialog))

    def test_submit_dialog_carries_the_stylesheet(self):
        from job_manager.submit_dialog import SubmitDialog

        self.service.store.add_host(
            __import__("job_manager.models", fromlist=["HostProfile"]).HostProfile(name="h")
        )
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(self._has_stylesheet(dialog))

    def test_every_window_title_names_the_plugin_and_version(self):
        # One shape for all of them: "Job Manager <version> - <what>".
        from job_manager import PLUGIN_VERSION

        for dialog in (JobsDialog(self.service), HostsDialog(self.service)):
            self.addCleanup(dialog.deleteLater)
            self.assertTrue(
                dialog.windowTitle().startswith(f"Job Manager {PLUGIN_VERSION} - "),
                dialog.windowTitle(),
            )

    def test_download_dialog_carries_the_stylesheet(self):
        dialog = DownloadDialog("mol", ["a.out", "b.log"], ["a.out"], self.tmp, "Download")
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(self._has_stylesheet(dialog))


class TestHostMonitorDialogTheme(HostMonitorTestCase):
    def test_dark_mode_toggle(self):
        dialog = self.monitor()
        dialog._set_dark(True)
        self.assertIn("#16181a", dialog.styleSheet())
        dialog._set_dark(False)
        # Not "": an empty stylesheet left buttons and fields on native
        # platform chrome, a different size than the dark style's own
        # padding -- toggling the button visibly resized it. Light mode has
        # its own explicit stylesheet with matching padding instead.
        self.assertIn("#f6f8fa", dialog.styleSheet())
        self.assertNotIn("#16181a", dialog.styleSheet())

    def test_window_title_says_host_monitor(self):
        dialog = self.monitor()
        self.assertIn("Host Monitor", dialog.windowTitle())


# ---------------------------------------------------------------------------
# State colours — jobs dialog
# ---------------------------------------------------------------------------


class TestStateColours(DialogTestCase):
    """_STATE_COLORS in jobs_dialog must use the accent palette."""

    def test_running_colour_matches_theme(self):
        from job_manager.jobs_dialog import _STATE_COLORS

        self.assertEqual(_STATE_COLORS[STATE_RUNNING], theme.CY_GREEN)

    def test_failed_colour_matches_theme(self):
        from job_manager.jobs_dialog import _STATE_COLORS

        self.assertEqual(_STATE_COLORS[STATE_FAILED], theme.CY_RED)

    def test_pending_colour_matches_theme(self):
        from job_manager.jobs_dialog import _STATE_COLORS

        self.assertEqual(_STATE_COLORS[STATE_PENDING], theme.CY_AMBER)

    def test_banner_style_has_accent_left_border(self):
        from job_manager.jobs_dialog import BANNER_STYLE

        self.assertIn(theme.CY_ACCENT2, BANNER_STYLE)
        self.assertIn("border-left", BANNER_STYLE)

    def test_interval_warning_uses_amber(self):
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        self.assertIn(theme.CY_AMBER, dialog.lbl_interval_warning.styleSheet())

    def test_host_monitor_button_renamed(self):
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("Host Monitor", dialog.btn_host_monitor.text())
        self.assertNotIn("Hosts at Work", dialog.btn_host_monitor.text())


class TestStateColumnStaysReadableWhenSelected(DialogTestCase):
    """Qt's own delegate paints selected text in HighlightedText, ignoring the
    model's ForegroundRole -- which is how a FAILED row went from red to the
    theme's ordinary (near-black in light mode) text colour the moment it was
    clicked, on top of the selection highlight."""

    def _index_for(self, dialog, state):
        from job_manager.models import Job

        job = Job(id="j1", name="mol", host_name="h", state=state, submitted_at=1000.0)
        self.service.store.jobs[job.id] = job
        dialog.model.reload()
        row = dialog.model.row_of(job.id)
        return dialog.model.index(row, 3)

    def test_the_table_uses_the_delegate_on_the_state_column(self):
        from job_manager.jobs_dialog import _StateColorDelegate

        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        self.assertIsInstance(dialog.table.itemDelegateForColumn(3), _StateColorDelegate)

    def test_selected_state_text_keeps_its_colour(self):
        from PyQt6.QtWidgets import QStyle, QStyleOptionViewItem
        from PyQt6.QtGui import QPalette

        from job_manager.jobs_dialog import _StateColorDelegate
        from job_manager.models import STATE_FAILED

        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        index = self._index_for(dialog, STATE_FAILED)

        delegate = _StateColorDelegate(dialog.table)
        option = QStyleOptionViewItem()
        option.state |= QStyle.StateFlag.State_Selected
        delegate.initStyleOption(option, index)

        expected = QColor(theme.CY_RED)
        self.assertEqual(option.palette.color(QPalette.ColorRole.Text), expected)
        self.assertEqual(option.palette.color(QPalette.ColorRole.HighlightedText), expected)

    def test_an_unstyled_column_is_left_alone(self):
        from PyQt6.QtWidgets import QStyleOptionViewItem

        from job_manager.jobs_dialog import _StateColorDelegate
        from job_manager.models import STATE_FAILED

        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        index = self._index_for(dialog, STATE_FAILED)
        name_index = dialog.model.index(index.row(), 0)

        delegate = _StateColorDelegate(dialog.table)
        before = QStyleOptionViewItem()
        delegate.initStyleOption(before, name_index)
        # No ForegroundRole on the Name column, so nothing to override with.
        default = QStyleOptionViewItem()
        self.assertEqual(
            before.palette.color(QPalette.ColorRole.Text),
            default.palette.color(QPalette.ColorRole.Text),
        )


# ---------------------------------------------------------------------------
# Status widget dark-mode fix
# ---------------------------------------------------------------------------


class TestStatusWidgetDarkModeFix(DialogTestCase):
    """The palette-based colour must survive a theme change."""

    def _make_widget(self):
        widget = JobStatusWidget(self.service)
        self.addCleanup(widget.deleteLater)
        return widget

    def test_widget_has_no_stylesheet_after_refresh(self):
        # A stylesheet would lock the colour in and break dark-mode.
        job = Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0)
        self.store.add_job(job)
        widget = self._make_widget()
        # The stylesheet must be empty (or at most the dialog-level one, which
        # is NOT applied here since this is a QLabel, not a dialog).
        self.assertEqual(widget.styleSheet(), "")

    def test_colour_is_in_the_palette_not_the_stylesheet(self):
        job = Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0)
        self.store.add_job(job)
        widget = self._make_widget()
        palette_color = widget.palette().color(
            QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText
        )
        # Should be the neon-green accent, not the default text colour.
        self.assertEqual(palette_color.name().lower(), theme.CY_GREEN.lower())

    def test_change_event_reapplies_colour(self):
        job = Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0)
        self.store.add_job(job)
        widget = self._make_widget()
        # Simulate a palette-change event (what Qt sends on theme switch).
        event = QEvent(QEvent.Type.PaletteChange)
        widget.changeEvent(event)
        palette_color = widget.palette().color(
            QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText
        )
        self.assertEqual(palette_color.name().lower(), theme.CY_GREEN.lower())

    def test_hidden_widget_has_no_colour(self):
        # No active jobs → widget is hidden and _color is cleared.
        widget = self._make_widget()
        self.assertEqual(widget._color, "")

    def test_blocked_job_uses_red(self):
        # A blocked job should show the red accent.
        job = Job(
            id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0, after_job_id="dead"
        )
        dead = Job(id="dead", name="prev", state=STATE_FAILED, submitted_at=900.0)
        self.store.add_job(dead)
        self.store.add_job(job)
        widget = self._make_widget()
        self.assertEqual(widget._color, theme.CY_RED)


# ---------------------------------------------------------------------------
# _ActiveJobsBar
# ---------------------------------------------------------------------------


class TestActiveJobsBar(HostMonitorTestCase):
    """The summary strip at the bottom of the Host Monitor."""

    def _bar(self):
        bar = _ActiveJobsBar(self.service)
        self.addCleanup(bar.deleteLater)
        return bar

    def test_shows_idle_when_no_active_jobs(self):
        bar = self._bar()
        self.assertIn("no active jobs", bar._lbl_count.text())

    def test_visible_when_a_job_is_active(self):
        self.store.add_job(Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("1 running", bar._lbl_count.text())

    def test_pending_jobs_count_as_waiting(self):
        self.store.add_job(Job(id="j1", name="mol", state=STATE_PENDING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("1 waiting", bar._lbl_count.text())

    def test_no_emoji_in_the_summary(self):
        self.store.add_job(Job(id="j1", name="mol", state=STATE_PENDING, submitted_at=1000.0))
        bar = self._bar()
        text = bar._lbl_count.text()
        self.assertNotIn("\u23f3", text)
        self.assertNotIn("\u231b", text)

    def test_updates_on_jobs_changed(self):
        bar = self._bar()
        self.assertIn("no active jobs", bar._lbl_count.text())
        self.store.add_job(Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0))
        self.service.jobs_changed.emit()
        bar.refresh()
        self.assertIn("running", bar._lbl_count.text())

    def test_updates_on_job_updated(self):
        from job_manager.models import STATE_DONE

        self.store.add_job(Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("running", bar._lbl_count.text())
        self.store.jobs["j1"].state = STATE_DONE
        self.store.invalidate_chains()
        self.service.job_updated.emit("j1")
        bar.refresh()
        self.assertIn("no active jobs", bar._lbl_count.text())

    def test_finished_progress_on_summary_bar(self):
        from job_manager.models import STATE_DONE

        self.store.add_job(Job(id="j1", name="done1", state=STATE_DONE, submitted_at=900.0))
        self.store.add_job(Job(id="j2", name="run1", state=STATE_RUNNING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("1 running", bar._lbl_count.text())
        self.assertIn("1/2 finished", bar._lbl_count.text())

    def test_bar_is_present_in_the_monitor_dialog(self):
        dialog = self.monitor()
        self.assertIsNotNone(dialog._jobs_bar)

    def test_bar_is_instance_of_active_jobs_bar(self):
        dialog = self.monitor()
        self.assertIsInstance(dialog._jobs_bar, _ActiveJobsBar)

    def test_running_and_waiting_shown_together(self):
        self.store.add_job(Job(id="j1", name="run", state=STATE_RUNNING, submitted_at=1000.0))
        self.store.add_job(Job(id="j2", name="pend", state=STATE_PENDING, submitted_at=1001.0))
        bar = self._bar()
        text = bar._lbl_count.text()
        self.assertIn("running", text)
        self.assertIn("waiting", text)


# ---------------------------------------------------------------------------
# HostCard per-card jobs strip
# ---------------------------------------------------------------------------


class TestHostCardJobsStrip(HostMonitorTestCase):
    """Each card says what its own host is doing, on exactly two lines."""

    def _card(self, width: int = 300):
        from job_manager.host_monitor import HostCard

        card = HostCard(self.host)
        card.resize(width, 320)
        # Shown so the layout gives the two lines a real width: that is what
        # decides whether a long name is elided.
        card.show()
        self.addCleanup(card.deleteLater)
        return card

    def _job(self, **overrides):
        fields = dict(state=STATE_RUNNING, host_name=self.host.name, submitted_at=1000.0)
        fields.update(overrides)
        return Job(**fields)

    def test_blank_by_default(self):
        card = self._card()
        self.assertEqual(card.lbl_job.text(), BLANK)
        self.assertEqual(card.lbl_job_counts.text(), BLANK)

    def test_no_markup_entity_is_ever_shown(self):
        card = self._card()
        for label in (card.lbl_job, card.lbl_job_counts, card.lbl_load_avg):
            self.assertNotIn("&nbsp;", label.text())

    def test_shows_running_job(self):
        card = self._card()
        card.show_jobs([self._job(id="j1", name="myrun")])
        self.assertIn("myrun", card.lbl_job.text())
        self.assertIn("running", card.lbl_job.text())
        self.assertIn("0/1 done", card.lbl_job_counts.text())

    def test_blank_when_there_are_no_jobs(self):
        card = self._card()
        card.show_jobs([])
        self.assertEqual(card.lbl_job.text(), BLANK)

    def test_a_job_being_submitted_is_shown(self):
        # The resubmit case: the job exists but the queue has not seen it yet.
        from job_manager.models import STATE_UPLOADING

        card = self._card()
        card.show_jobs([self._job(id="j1", name="fresh", state=STATE_UPLOADING)])
        self.assertIn("fresh", card.lbl_job.text())
        self.assertIn("submitting", card.lbl_job.text())

    def test_a_finished_job_still_names_itself(self):
        from job_manager.models import STATE_DONE

        card = self._card()
        card.show_jobs([self._job(id="j1", name="allover", state=STATE_DONE)])
        self.assertIn("allover", card.lbl_job.text())
        self.assertIn("finished", card.lbl_job.text())

    def test_a_long_name_is_elided_not_wrapped(self):
        card = self._card(width=200)
        before = card.lbl_job.height()
        card.show_jobs([self._job(id="j1", name="z" * 120)])
        self.assertEqual(card.lbl_job.height(), before)
        self.assertFalse(card.lbl_job.wordWrap())
        self.assertNotIn("z" * 120, card.lbl_job.text())

    def test_both_lines_keep_their_height_whatever_the_name(self):
        short = self._card(width=220)
        short.show_jobs([self._job(id="j1", name="a")])
        wide = self._card(width=220)
        wide.show_jobs([self._job(id="j2", name="b" * 200)])
        self.assertEqual(short.lbl_job.height(), wide.lbl_job.height())
        self.assertEqual(short.lbl_job_counts.height(), wide.lbl_job_counts.height())

    def test_running_wins_over_everything_else(self):
        jobs = [self._job(id=f"j{i}", name=f"job{i}", submitted_at=float(i)) for i in range(8)]
        card = self._card()
        card.show_jobs(jobs)
        self.assertIn("job7", card.lbl_job.text())
        self.assertIn("8 active", card.lbl_job_counts.text())

    def test_a_queued_job_is_marked_with_the_text_hourglass(self):
        card = self._card()
        card.show_jobs([self._job(id="j1", name="pendingjob", state=STATE_PENDING)])
        self.assertIn("queued", card.lbl_job.text())
        self.assertIn(HOURGLASS, card.lbl_job.text())

    def test_dialog_wires_refresh_on_open(self):
        self.store.add_job(self._job(id="j1", name="mol", host_id=self.host.id))
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertIn("mol", card.lbl_job.text())

    def test_finished_counter(self):
        from job_manager.models import STATE_DONE

        card = self._card()
        card.show_jobs(
            [
                self._job(id="j1", name="job1", state=STATE_DONE),
                self._job(id="j2", name="job2", submitted_at=1001.0),
                self._job(id="j3", name="job3", state=STATE_PENDING, submitted_at=1002.0),
            ]
        )
        self.assertIn("job2", card.lbl_job.text())
        self.assertIn("1/3 done", card.lbl_job_counts.text())

    def test_card_updates_on_jobs_changed(self):
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertEqual(card.lbl_job.text(), BLANK)
        self.store.add_job(self._job(id="j1", name="newjob", host_id=self.host.id))
        self.service.jobs_changed.emit()
        dialog._refresh_card_jobs()
        self.assertIn("newjob", card.lbl_job.text())


# ---------------------------------------------------------------------------
# Spin box up/down arrow controls
# ---------------------------------------------------------------------------


class TestSpinBoxArrowControls(DialogTestCase):
    """QSpinBox up and down buttons click and adjust values correctly."""

    def test_spinbox_up_and_down_clicks_with_theme(self):
        from PyQt6.QtWidgets import QSpinBox
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import Qt, QPoint

        spin = QSpinBox()
        self.addCleanup(spin.deleteLater)
        spin.setStyleSheet(theme.DIALOG_STYLESHEET)
        spin.setRange(1, 100)
        spin.setValue(10)
        spin.resize(100, 26)
        spin.show()

        # Click top right (up arrow)
        QTest.mouseClick(
            spin,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(spin.width() - 6, 4),
        )
        self.assertEqual(spin.value(), 11)

        # Click bottom right (down arrow)
        QTest.mouseClick(
            spin,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(spin.width() - 6, spin.height() - 4),
        )
        self.assertEqual(spin.value(), 10)
