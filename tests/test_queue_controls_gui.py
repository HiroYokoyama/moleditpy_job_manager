"""The host editor's queue controls, against real Qt widgets.

``pause_command`` shipped implemented and tested, and no user could reach it:
nothing outside the runner modules called it, while the docs listed pause as a
feature of the helper. These tests are the wiring that closes that gap.
"""

from __future__ import annotations

import unittest

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager.hosts_dialog import HostsDialog  # noqa: E402
from job_manager.models import (  # noqa: E402
    BACKEND_PARAMIKO,
    MODE_LANES,
    MODE_RUNNER,
    SCHEDULER_SHELL,
)
from job_manager.remote_runner import PAUSED_NAME  # noqa: E402

from .fakes import FakeTransport, make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class QueueControlTestCase(DialogTestCase):
    def runner_host(self, **overrides):
        defaults = dict(
            id="runner1",
            name="workstation",
            scheduler=SCHEDULER_SHELL,
            concurrency_mode=MODE_RUNNER,
            max_concurrent=2,
            runner_cores=8,
        )
        defaults.update(overrides)
        host = make_host(**defaults)
        self.store.add_host(host)
        return host

    def dialog(self) -> HostsDialog:
        dialog = HostsDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def select(self, dialog: HostsDialog, host_id: str) -> None:
        from PyQt6.QtCore import Qt

        for row in range(dialog.list.count()):
            if dialog.list.item(row).data(Qt.ItemDataRole.UserRole) == host_id:
                dialog.list.setCurrentRow(row)
                return
        self.fail(f"{host_id} is not in the list")


class TestTheControlsAppearOnlyWhereThereIsAQueue(QueueControlTestCase):
    def test_a_helper_host_gets_them(self):
        self.runner_host()
        dialog = self.dialog()
        self.select(dialog, "runner1")
        self.assertTrue(dialog.queue_box.isVisibleTo(dialog))

    def test_a_chained_host_does_not(self):
        # Chaining leaves nothing on the host, so there is nothing to hold.
        self.runner_host(concurrency_mode=MODE_LANES)
        dialog = self.dialog()
        self.select(dialog, "runner1")
        self.assertFalse(dialog.queue_box.isVisibleTo(dialog))

    def test_a_cluster_does_not(self):
        # SLURM schedules for itself; the helper is never offered there.
        dialog = self.dialog()
        self.select(dialog, "host1")
        self.assertFalse(dialog.queue_box.isVisibleTo(dialog))

    def test_switching_the_mode_shows_them_at_once(self):
        self.runner_host(concurrency_mode=MODE_LANES)
        dialog = self.dialog()
        self.select(dialog, "runner1")

        dialog.cmb_concurrency.setCurrentIndex(dialog.cmb_concurrency.findData(MODE_RUNNER))

        self.assertTrue(dialog.queue_box.isVisibleTo(dialog))


class TestReadingTheQueueOnSelection(QueueControlTestCase):
    def test_a_held_queue_ticks_the_box(self):
        self.runner_host()
        self.transport.when(PAUSED_NAME, stdout="paused\n")
        dialog = self.dialog()

        self.select(dialog, "runner1")

        self.assertTrue(dialog.chk_pause.isChecked())

    def test_a_moving_queue_leaves_it_clear(self):
        self.runner_host()
        self.transport.when(PAUSED_NAME, stdout="running\n")
        dialog = self.dialog()

        self.select(dialog, "runner1")

        self.assertFalse(dialog.chk_pause.isChecked())

    def test_showing_the_state_does_not_ask_the_host_to_change_it(self):
        # The box is set to match the host. Without the guard, that set fires
        # toggled and sends a pause command for a queue that is already held.
        self.runner_host()
        self.transport.when(PAUSED_NAME, stdout="paused\n")
        dialog = self.dialog()

        self.select(dialog, "runner1")

        self.assertFalse(self.transport.ran("touch"))

    def test_a_cluster_is_never_asked_at_all(self):
        dialog = self.dialog()
        self.select(dialog, "host1")
        self.assertFalse(self.transport.ran(PAUSED_NAME))

    def test_a_host_that_would_prompt_for_a_password_is_left_alone(self):
        # Clicking a name in a list must not raise a password dialog.
        self.runner_host(backend=BACKEND_PARAMIKO, ask_password=True)
        dialog = self.dialog()

        self.select(dialog, "runner1")

        self.assertFalse(self.transport.ran(PAUSED_NAME))
        self.assertFalse(dialog.chk_pause.isEnabled())

    def test_the_same_host_is_not_asked_twice(self):
        # Saving reloads the list, which re-selects, which lands back here.
        # Pressing Save must not put another command on the wire.
        self.runner_host()
        self.transport.when(PAUSED_NAME, stdout="running\n")
        dialog = self.dialog()
        self.select(dialog, "runner1")
        before = self.transport.count_matching(PAUSED_NAME)

        dialog._save_current()

        self.assertEqual(self.transport.count_matching(PAUSED_NAME), before)

    def test_a_different_host_is_asked(self):
        self.runner_host()
        self.runner_host(id="runner2", name="other")
        self.transport.when(PAUSED_NAME, stdout="running\n")
        dialog = self.dialog()
        self.select(dialog, "runner1")
        before = self.transport.count_matching(PAUSED_NAME)

        self.select(dialog, "runner2")

        self.assertGreater(self.transport.count_matching(PAUSED_NAME), before)

    def test_an_unreachable_host_says_so_rather_than_hanging(self):
        self.runner_host()
        self.transport.when(PAUSED_NAME, rc=255, stderr="ssh: connect: timed out")
        dialog = self.dialog()

        self.select(dialog, "runner1")

        self.assertTrue(dialog.chk_pause.isEnabled())


class TestHoldingAndReleasing(QueueControlTestCase):
    def setUp(self):
        super().setUp()
        self.runner_host()
        self.dlg = self.dialog()
        self.select(self.dlg, "runner1")
        self.transport.commands.clear()

    def test_ticking_it_holds_the_queue(self):
        self.dlg.chk_pause.setChecked(True)
        self.assertTrue(self.transport.ran("touch"))
        self.assertTrue(self.transport.ran(PAUSED_NAME))

    def test_clearing_it_lets_the_queue_run(self):
        self.dlg.chk_pause.setChecked(True)
        self.transport.commands.clear()

        self.dlg.chk_pause.setChecked(False)

        self.assertTrue(self.transport.ran("rm -f"))

    def test_the_label_says_running_jobs_are_untouched(self):
        # Users hesitate over a pause button that might kill six hours of work.
        self.dlg.chk_pause.setChecked(True)
        self.assertIn("running continue", self.dlg.lbl_queue.text().lower())

    def test_a_refusal_puts_the_box_back(self):
        # Leaving it ticked would have the user believe a queue is held when it
        # is still handing out jobs.
        self.transport.when(PAUSED_NAME, rc=1, stderr="read-only file system")

        self.dlg.chk_pause.setChecked(True)

        self.assertFalse(self.dlg.chk_pause.isChecked())
        self.assertIn("read-only", self.dlg.lbl_queue.text())

    def test_the_failure_message_does_not_re_send_the_command(self):
        # Putting the box back must not itself count as a user toggle.
        self.transport.when(PAUSED_NAME, rc=1, stderr="nope")
        self.dlg.chk_pause.setChecked(True)
        self.assertEqual(self.transport.count_matching("rm -f"), 0)


class TestSendingTheLimits(QueueControlTestCase):
    def setUp(self):
        super().setUp()
        self.runner_host()
        self.dlg = self.dialog()
        self.select(self.dlg, "runner1")
        self.transport.commands.clear()

    def test_the_edited_limits_are_what_get_sent(self):
        # The point of the button: a limit changed between submissions used to
        # do nothing until the next one.
        self.dlg.spin_max_concurrent.setValue(5)
        self.dlg.spin_runner_cores.setValue(40)

        self.dlg._apply_queue_limits()

        self.assertTrue(self.transport.ran("5"))
        self.assertTrue(self.transport.ran("40"))

    def test_the_edit_is_saved_as_well_as_sent(self):
        self.dlg.spin_max_concurrent.setValue(5)

        self.dlg._apply_queue_limits()

        self.assertEqual(self.store.hosts["runner1"].max_concurrent, 5)

    def test_the_label_reports_what_the_helper_will_do(self):
        self.dlg.spin_max_concurrent.setValue(3)
        self.dlg.spin_runner_cores.setValue(16)

        self.dlg._apply_queue_limits()

        text = self.dlg.lbl_queue.text()
        self.assertIn("3", text)
        self.assertIn("16", text)

    def test_detect_is_described_rather_than_printed_as_zero(self):
        self.dlg.spin_runner_cores.setValue(0)
        self.dlg._apply_queue_limits()
        self.assertIn("every core", self.dlg.lbl_queue.text())

    def test_the_button_comes_back_after_a_failure(self):
        self.transport.default = FakeTransport().default
        self.transport.when("mkdir -p", rc=1, stderr="no such host")

        self.dlg._apply_queue_limits()

        self.assertTrue(self.dlg.btn_apply_limits.isEnabled())


class TestSelectionRaces(QueueControlTestCase):
    def test_an_answer_for_a_host_no_longer_selected_is_dropped(self):
        # The reply describes the host that was selected when it was sent.
        self.runner_host()
        self.transport.when(PAUSED_NAME, stdout="paused\n")
        dialog = self.dialog()
        self.select(dialog, "runner1")

        dialog._current = self.store.hosts["host1"]
        dialog._refresh_queue_state()

        self.assertFalse(dialog.chk_pause.isChecked())


if __name__ == "__main__":
    unittest.main()
