"""The host editor's queue controls, against real Qt widgets.

``pause_command`` shipped implemented and tested, and no user could reach it:
nothing outside the runner modules called it, while the docs listed pause as a
feature of the helper. These tests are the wiring that closes that gap.
"""

from __future__ import annotations

import os
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

from .fakes import FakeTransport, make_host, make_preset  # noqa: E402
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
        # "Ask the host" is the checkbox now; the fields cannot be 0 by hand.
        self.dlg.chk_detect_resources.setChecked(True)
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


class TestTheWizardReadsTheInput(DialogTestCase):
    """Filling Memory and CPUs from the file the user just added."""

    def dialog(self):
        from job_manager.submit_dialog import SubmitDialog

        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def orca_input(self, name="mol.inp"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("! B3LYP Opt\n%pal nprocs 8 end\n%maxcore 3000\n* xyz 0 1\nH 0 0 0\n*\n")
        return path

    def test_the_memory_request_is_filled_in(self):
        dialog = self.dialog()

        dialog.add_files([self.orca_input()])

        # 3000 MB per core times eight cores, not 3000.
        self.assertEqual(dialog.txt_memory.text(), "24000M")

    def test_the_core_request_is_filled_in(self):
        dialog = self.dialog()
        dialog.add_files([self.orca_input()])
        self.assertEqual(dialog.spin_cpus.value(), 8)

    def test_the_user_is_told_where_the_numbers_came_from(self):
        dialog = self.dialog()
        dialog.add_files([self.orca_input()])
        self.assertTrue(dialog.lbl_scanned.isVisibleTo(dialog))
        self.assertIn("ORCA", dialog.lbl_scanned.text())

    def test_a_value_already_typed_is_never_written_over(self):
        # A filled field is a decision; a guess must not replace it.
        dialog = self.dialog()
        dialog.txt_memory.setText("4G")
        dialog.spin_cpus.setValue(2)

        dialog.add_files([self.orca_input()])

        self.assertEqual(dialog.txt_memory.text(), "4G")
        self.assertEqual(dialog.spin_cpus.value(), 2)

    def test_a_prefilled_wizard_reads_the_input_too(self):
        # Every way into the wizard bar "Add files..." goes through prefill: a
        # drop on the monitor, the input generators' Submit to Cluster, and
        # Resubmit. Reading the input only on the button meant those three
        # asked the queue for one core and no memory.
        dialog = self.dialog()

        dialog.prefill(files=[self.orca_input()])

        self.assertEqual(dialog.spin_cpus.value(), 8)
        self.assertEqual(dialog.txt_memory.text(), "24000M")

    def test_a_resubmit_keeps_the_numbers_it_ran_with(self):
        # The preset is applied before the scan, and the scan only fills a
        # field still at its default, so a job resubmitted with 2 CPUs stays
        # at 2 rather than being "corrected" from the file.
        dialog = self.dialog()

        dialog.prefill(
            files=[self.orca_input()],
            preset={"cpus_per_task": 2, "memory": "4G", "command_template": "orca {input}"},
        )

        self.assertEqual(dialog.spin_cpus.value(), 2)
        self.assertEqual(dialog.txt_memory.text(), "4G")

    def test_unticking_the_box_leaves_both_fields_alone(self):
        dialog = self.dialog()
        dialog.chk_scan_resources.setChecked(False)

        dialog.add_files([self.orca_input()])

        self.assertEqual(dialog.txt_memory.text(), "")
        self.assertEqual(dialog.spin_cpus.value(), 1)
        self.assertFalse(dialog.lbl_scanned.isVisibleTo(dialog))

    def test_the_choice_is_remembered(self):
        dialog = self.dialog()
        dialog.chk_scan_resources.setChecked(False)
        self.assertFalse(self.store.get_pref("scan_resources", True))
        self.assertFalse(self.dialog().chk_scan_resources.isChecked())

    def test_ticking_it_reads_the_file_already_chosen(self):
        # Otherwise the box only takes effect on the next file added, and the
        # one already in the list stays unread with no way to ask for it.
        dialog = self.dialog()
        dialog.chk_scan_resources.setChecked(False)
        dialog.add_files([self.orca_input()])

        dialog.chk_scan_resources.setChecked(True)

        self.assertEqual(dialog.spin_cpus.value(), 8)

    def test_the_command_is_not_rewritten_by_the_scan(self):
        # The scan fills resources only: a command the user chose or typed is
        # never touched by what the input file says about cores.
        dialog = self.dialog()
        dialog.txt_command.setText("orca {input} > {stem}.out")

        dialog.add_files([self.orca_input()])

        self.assertEqual(dialog.txt_command.text(), "orca {input} > {stem}.out")

    def test_a_file_that_states_nothing_changes_nothing(self):
        path = os.path.join(self.tmp, "mol.xyz")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1\nx\nH 0 0 0\n")
        dialog = self.dialog()

        dialog.add_files([path])

        self.assertEqual(dialog.txt_memory.text(), "")
        self.assertFalse(dialog.lbl_scanned.isVisibleTo(dialog))

    def test_the_request_reaches_the_preset_that_is_submitted(self):
        dialog = self.dialog()
        dialog.add_files([self.orca_input()])

        preset = dialog.collect_preset()

        self.assertEqual(preset.memory, "24000M")
        self.assertEqual(preset.cpus_per_task, 8)


class TestTheWizardRemembersTheLastSubmission(DialogTestCase):
    """Site settings come back; what the molecule decides does not."""

    def dialog(self):
        from job_manager.submit_dialog import SubmitDialog

        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def submit_once(self, **fields):
        dialog = self.dialog()
        dialog.txt_walltime.setText(fields.get("walltime", "99:00:00"))
        dialog.txt_queue.setText(fields.get("queue", "debug"))
        dialog.txt_modules.setPlainText(fields.get("modules", "orca/5"))
        dialog.txt_globs.setText(fields.get("globs", "*.out, *.gbw"))
        dialog.txt_command.setText("orca {input} > {stem}.out")
        dialog.chk_auto_download.setChecked(False)
        dialog.spin_cpus.setValue(12)
        dialog._remember(self.host, dialog.collect_preset())
        return dialog

    def test_the_site_settings_come_back(self):
        self.submit_once()

        again = self.dialog()

        self.assertEqual(again.txt_walltime.text(), "99:00:00")
        self.assertEqual(again.txt_queue.text(), "debug")
        self.assertEqual(again.txt_modules.toPlainText(), "orca/5")
        self.assertEqual(again.txt_globs.text(), "*.out, *.gbw")
        self.assertFalse(again.chk_auto_download.isChecked())

    def test_cores_are_left_for_the_input_to_decide(self):
        # With the scan ticked, the molecule decides these two -- carrying the
        # last job's twelve cores onto a different molecule is how a job ends
        # up asking for resources it does not need.
        self.submit_once()

        again = self.dialog()

        self.assertTrue(again.chk_scan_resources.isChecked())
        self.assertEqual(again.spin_cpus.value(), 1)
        self.assertEqual(again.txt_memory.text(), "")

    def test_with_the_scan_off_the_numbers_come_back_too(self):
        self.store.set_pref("scan_resources", False)
        self.submit_once()

        again = self.dialog()

        self.assertEqual(again.spin_cpus.value(), 12)

    def test_a_named_preset_wins_over_the_remembered_one(self):
        # Choosing a preset is a decision; last time's settings must not
        # quietly overwrite it.
        self.submit_once()
        preset = make_preset(host_id=self.host.id, name="mine", walltime="01:00:00")
        self.store.add_preset(preset)

        again = self.dialog()

        self.assertEqual(again.txt_walltime.text(), "01:00:00")

    def test_nothing_is_remembered_for_a_different_host(self):
        from job_manager.models import HostProfile

        self.submit_once()
        other = HostProfile(id="other", name="second", hostname="b.example.org")
        self.store.add_host(other)

        again = self.dialog()
        again.cmb_host.setCurrentIndex(again.cmb_host.findData("other"))

        self.assertNotEqual(again.txt_walltime.text(), "99:00:00")
