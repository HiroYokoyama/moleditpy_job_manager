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
from job_manager.host_monitor import _ActiveJobsBar  # noqa: E402
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

    def test_window_title_says_hosts_monitor(self):
        dialog = self.monitor()
        self.assertIn("Hosts Monitor", dialog.windowTitle())


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

    def test_hosts_monitor_button_renamed(self):
        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("Hosts Monitor", dialog.btn_host_monitor.text())
        self.assertNotIn("Hosts at Work", dialog.btn_host_monitor.text())


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
    """The running-jobs strip at the bottom of the Hosts Monitor."""

    def _bar(self):
        bar = _ActiveJobsBar(self.service)
        self.addCleanup(bar.deleteLater)
        return bar

    def test_shows_idle_when_no_active_jobs(self):
        # Bar stays visible and shows a quiet idle message rather than hiding.
        bar = self._bar()
        self.assertIn("no active jobs", bar._lbl_count.text())

    def test_visible_when_a_job_is_active(self):
        self.store.add_job(Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("running", bar._lbl_count.text())

    def test_running_count_is_shown(self):
        self.store.add_job(Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0))
        bar = self._bar()
        # The label uses HTML; check the plain text contains the count.
        self.assertIn("1", bar._lbl_count.text())
        self.assertIn("running", bar._lbl_count.text())

    def test_pending_jobs_count_as_remaining(self):
        self.store.add_job(Job(id="j1", name="mol", state=STATE_PENDING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("remaining", bar._lbl_count.text())

    def test_updates_on_jobs_changed(self):
        bar = self._bar()
        self.assertIn("no active jobs", bar._lbl_count.text())
        self.store.add_job(Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0))
        self.service.jobs_changed.emit()
        self.assertIn("running", bar._lbl_count.text())

    def test_updates_on_job_updated(self):
        from job_manager.models import STATE_DONE

        self.store.add_job(Job(id="j1", name="mol", state=STATE_RUNNING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("running", bar._lbl_count.text())
        # is_active is a derived property; transition to a terminal state
        # to make the job inactive and verify the bar returns to idle.
        self.store.jobs["j1"].state = STATE_DONE
        self.service.job_updated.emit("j1")
        self.assertIn("no active jobs", bar._lbl_count.text())

    def test_task_done_progress_on_summary_bar(self):
        from job_manager.models import STATE_DONE

        self.store.add_job(Job(id="j1", name="done1", state=STATE_DONE, submitted_at=900.0))
        self.store.add_job(Job(id="j2", name="run1", state=STATE_RUNNING, submitted_at=1000.0))
        bar = self._bar()
        self.assertIn("1 running", bar._lbl_count.text())
        self.assertIn("task 1/2 done", bar._lbl_count.text())

    def test_bar_is_present_in_the_monitor_dialog(self):
        dialog = self.monitor()
        self.assertIsNotNone(dialog._jobs_bar)

    def test_bar_is_instance_of_active_jobs_bar(self):
        dialog = self.monitor()
        self.assertIsInstance(dialog._jobs_bar, _ActiveJobsBar)

    def test_running_and_remaining_shown_together(self):
        self.store.add_job(Job(id="j1", name="run", state=STATE_RUNNING, submitted_at=1000.0))
        self.store.add_job(Job(id="j2", name="pend", state=STATE_PENDING, submitted_at=1001.0))
        bar = self._bar()
        text = bar._lbl_count.text()
        # One job is running, one is pending/remaining -- both must appear.
        self.assertIn("running", text)
        self.assertIn("remaining", text)


# ---------------------------------------------------------------------------
# HostCard per-card jobs strip
# ---------------------------------------------------------------------------


class TestHostCardJobsStrip(HostMonitorTestCase):
    """Each card shows its own host's active jobs inline."""

    def _card(self):
        from job_manager.host_monitor import HostCard

        card = HostCard(self.host)
        self.addCleanup(card.deleteLater)
        return card

    def test_hidden_by_default(self):
        card = self._card()
        self.assertEqual(card.lbl_jobs.text(), "")
        self.assertTrue(card.lbl_jobs.isHidden())

    def test_shows_running_job(self):
        job = Job(
            id="j1",
            name="myrun",
            state=STATE_RUNNING,
            host_name=self.host.name,
            submitted_at=1000.0,
        )
        card = self._card()
        card.show_jobs([job])
        self.assertFalse(card.lbl_jobs.isHidden())
        self.assertIn("myrun", card.lbl_jobs.text())
        self.assertIn("running", card.lbl_jobs.text())

    def test_hidden_when_no_active_jobs(self):
        from job_manager.models import STATE_DONE

        job = Job(
            id="j1", name="done", state=STATE_DONE, host_name=self.host.name, submitted_at=1000.0
        )
        card = self._card()
        card.show_jobs([job])
        self.assertEqual(card.lbl_jobs.text(), "")
        self.assertTrue(card.lbl_jobs.isHidden())

    def test_long_name_is_truncated(self):
        long_name = "z" * 40
        job = Job(
            id="j1",
            name=long_name,
            state=STATE_RUNNING,
            host_name=self.host.name,
            submitted_at=1000.0,
        )
        card = self._card()
        card.show_jobs([job])
        self.assertNotIn(long_name, card.lbl_jobs.text())
        self.assertIn("…", card.lbl_jobs.text())

    def test_overflow_label_beyond_five(self):
        jobs = [
            Job(
                id=f"j{i}",
                name=f"job{i}",
                state=STATE_RUNNING,
                host_name=self.host.name,
                submitted_at=float(i),
            )
            for i in range(8)
        ]
        card = self._card()
        card.show_jobs(jobs)
        self.assertIn("more", card.lbl_jobs.text())

    def test_colour_matches_state(self):
        job = Job(
            id="j1",
            name="pendingjob",
            state=STATE_PENDING,
            host_name=self.host.name,
            submitted_at=1000.0,
        )
        card = self._card()
        card.show_jobs([job])
        self.assertIn(STATE_PENDING.lower(), card.lbl_jobs.text())

    def test_dialog_wires_refresh_on_open(self):
        self.store.add_job(
            Job(
                id="j1",
                name="mol",
                state=STATE_RUNNING,
                host_name=self.host.name,
                submitted_at=1000.0,
            )
        )
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertIn("mol", card.lbl_jobs.text())
        self.assertFalse(card.lbl_jobs.isHidden())

    def test_task_done_progress_counter(self):
        from job_manager.models import STATE_DONE

        j_done = Job(
            id="j1", name="job1", state=STATE_DONE, host_name=self.host.name, submitted_at=1000.0
        )
        j_run = Job(
            id="j2", name="job2", state=STATE_RUNNING, host_name=self.host.name, submitted_at=1001.0
        )
        j_pend = Job(
            id="j3", name="job3", state=STATE_PENDING, host_name=self.host.name, submitted_at=1002.0
        )
        card = self._card()
        card.show_jobs([j_done, j_run, j_pend])
        text = card.lbl_jobs.text()
        self.assertIn("1 running", text)
        self.assertIn("1 remaining", text)
        self.assertIn("task 1/3 done", text)

    def test_card_updates_on_jobs_changed(self):
        dialog = self.monitor()
        card = dialog.cards[self.host.id]
        self.assertEqual(card.lbl_jobs.text(), "")
        self.assertTrue(card.lbl_jobs.isHidden())
        self.store.add_job(
            Job(
                id="j1",
                name="newjob",
                state=STATE_RUNNING,
                host_name=self.host.name,
                submitted_at=1000.0,
            )
        )
        self.service.jobs_changed.emit()
        self.assertIn("newjob", card.lbl_jobs.text())
        self.assertFalse(card.lbl_jobs.isHidden())


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
