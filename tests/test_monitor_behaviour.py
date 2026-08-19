"""The monitor's newer promises: what a double click means, what a rebuilt list
refuses, what cancelling one job of a chain does to the rest, and how soon a
just-submitted job is looked at.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import (  # noqa: E402
    MODE_RUNNER,
    SCHEDULER_SHELL,
    STATE_DONE,
    STATE_PENDING,
    STATE_RUNNING,
    Job,
)

from .fakes import make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TestWhatADoubleClickOpens(DialogTestCase):
    """The log while it runs, the result once it has finished."""

    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        self.host = make_host()
        self.store.add_host(self.host)

    def add(self, **overrides) -> Job:
        fields = dict(
            id="j1",
            name="mol",
            host_id=self.host.id,
            host_name=self.host.name,
            remote_dir="/tmp/job",
            log_file="job.log",
            submitted_at=1000.0,
        )
        fields.update(overrides)
        job = Job(**fields)
        self.store.add_job(job)
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        self.dialog._update_buttons()
        return job

    def test_a_running_job_opens_its_log(self):
        self.add(state=STATE_RUNNING)
        with (
            patch.object(JobsDialog, "_tail_selected") as tail,
            patch.object(JobsDialog, "_open_selected_result") as result,
        ):
            self.dialog._open_double_clicked()
        tail.assert_called_once()
        result.assert_not_called()

    def test_a_finished_job_with_files_opens_the_result(self):
        path = os.path.join(self.tmp, "mol.out")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("energy\n")
        self.add(state=STATE_DONE, downloaded=True, downloaded_files=[path])
        with (
            patch.object(JobsDialog, "_tail_selected") as tail,
            patch.object(JobsDialog, "_open_selected_result") as result,
        ):
            self.dialog._open_double_clicked()
        result.assert_called_once()
        tail.assert_not_called()

    def test_a_finished_job_with_nothing_fetched_falls_back_to_the_log(self):
        # Open Result is disabled with nothing to open, and a double click must
        # still do something rather than nothing.
        self.add(state=STATE_DONE)
        with (
            patch.object(JobsDialog, "_tail_selected") as tail,
            patch.object(JobsDialog, "_open_selected_result") as result,
        ):
            self.dialog._open_double_clicked()
        tail.assert_called_once()
        result.assert_not_called()


class TestARebuiltListIsReadOnly(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.deleteLater)
        path = os.path.join(self.tmp, "rebuilt.pmejbs")
        self.store.write_job_list(
            path, [Job(id="j1", name="found", state=STATE_DONE, downloaded_files=["x.out"])], True
        )
        self.store.use_jobs_file(path)
        self.dialog.model.reload()
        self.dialog.table.selectRow(0)
        self.dialog._update_buttons()

    def test_the_window_says_so(self):
        self.dialog._update_active_file()
        self.assertIn("Rebuilt", self.dialog.lbl_active_file.text())

    def test_nothing_can_be_submitted(self):
        self.assertFalse(self.dialog.btn_new.isEnabled())
        self.assertFalse(self.dialog.btn_resubmit.isEnabled())

    def test_nothing_can_be_cancelled_or_polled(self):
        for button in (self.dialog.btn_cancel, self.dialog.btn_tail, self.dialog.btn_download):
            self.assertFalse(button.isEnabled())

    def test_the_result_can_still_be_opened(self):
        self.assertTrue(self.dialog.btn_open.isEnabled())

    def test_the_wizard_refuses_to_open(self):
        with patch.object(QMessageBox, "information") as told:
            with patch("job_manager.submit_dialog.SubmitDialog") as wizard:
                self.dialog.open_submit_dialog()
        told.assert_called_once()
        wizard.assert_not_called()


class TestCancellingOneJobOfAChain(DialogTestCase):
    """Cancelling the middle job must not throw away the ones behind it."""

    def setUp(self):
        super().setUp()
        self.host = make_host(scheduler=SCHEDULER_SHELL, concurrency_mode=MODE_RUNNER)
        self.store.add_host(self.host)
        self.first = Job(
            id="a",
            name="first",
            host_id=self.host.id,
            state=STATE_RUNNING,
            remote_job_id="0001_a.sh",
            submitted_at=1.0,
        )
        self.second = Job(
            id="b",
            name="second",
            host_id=self.host.id,
            state=STATE_PENDING,
            remote_job_id="0002_b.sh",
            after_job_id="a",
            submitted_at=2.0,
        )
        for job in (self.first, self.second):
            self.store.add_job(job)

    def test_the_job_behind_is_released_on_the_host(self):
        commands = []

        class Recorder:
            host = self.host

            def run(self, command, timeout=None):
                commands.append(command)
                from job_manager.transport.base import CommandResult

                return CommandResult(0, "", "")

            def close(self):
                pass

        with patch.object(self.service, "transport_for", return_value=Recorder()):
            self.service.cancel(self.first)
        self.assertTrue(
            any("queue/0002_b.sh" in command for command in commands),
            f"the dependent was never released: {commands}",
        )

    def test_the_job_behind_stops_being_blocked(self):
        with patch.object(self.service, "transport_for"):
            self.service.cancel(self.first)
        self.assertTrue(self.second.chain_any)

    def test_the_choice_is_remembered_across_a_restart(self):
        with patch.object(self.service, "transport_for"):
            self.service.cancel(self.first)
        from job_manager.store import JobStore

        reloaded = JobStore(directory=self.store.directory)
        self.assertTrue(reloaded.jobs["b"].chain_any)

    def test_cancelling_the_chain_releases_nothing(self):
        with patch.object(self.service, "transport_for"):
            self.service.cancel(self.first, release_dependents=False)
        self.assertFalse(self.second.chain_any)


class TestTheFirstPollAfterASubmission(DialogTestCase):
    def test_a_submission_asks_the_host_again_soon(self):
        poller = self.service.poller
        poller._next_poll["h"] = 1e12  # not due for a very long time
        self.store.add_job(Job(id="j1", host_id="h", state=STATE_RUNNING, submitted_at=1.0))

        poller.prime("h")

        self.assertNotIn("h", poller._next_poll)
        self.assertTrue(poller._kickoff.isActive())
        self.assertLessEqual(poller._kickoff.interval(), 10_000)

    def test_nothing_is_asked_when_no_job_is_active(self):
        poller = self.service.poller
        with patch.object(poller, "tick") as tick:
            poller._on_kickoff()
        tick.assert_not_called()

    def test_the_wait_is_short_enough_to_catch_the_first_status(self):
        from job_manager.poller import FIRST_POLL_SECONDS

        self.assertLessEqual(FIRST_POLL_SECONDS, 5.0)

    def test_shutdown_stops_the_kickoff(self):
        poller = self.service.poller
        poller.prime("h")
        poller.shutdown()
        self.assertFalse(poller._kickoff.isActive())

    def test_every_submission_gets_its_own_query_not_just_the_first(self):
        # A second job handed over while the first one's kickoff was still
        # pending used to be covered by that one alone -- which can fire a
        # moment after the second submission, before the queue knows anything,
        # putting its real first status a whole interval away again.
        poller = self.service.poller
        self.store.add_job(Job(id="j1", host_id="h", state=STATE_RUNNING, submitted_at=1.0))
        poller.prime("h")
        poller.prime("h")  # the second submission, while the kickoff is pending

        with patch.object(poller, "tick") as tick:
            poller._on_kickoff()
        tick.assert_called_once()
        self.assertTrue(poller._kickoff.isActive(), "no follow-up query was scheduled")

    def test_the_follow_up_makes_the_host_due_again(self):
        # The first pass polls the host, which puts it back on the normal
        # interval; without clearing that, the second pass would skip the very
        # host the newest job was submitted to.
        poller = self.service.poller
        self.store.add_job(Job(id="j1", host_id="h", state=STATE_RUNNING, submitted_at=1.0))
        poller.prime("h")
        with patch.object(poller, "tick"):
            poller._on_kickoff()
        poller._next_poll["h"] = 1e12  # as a finished poll would leave it

        with patch.object(poller, "tick"):
            poller._on_kickoff()

        self.assertNotIn("h", poller._next_poll)

    def test_the_burst_stops_once_no_more_jobs_are_handed_over(self):
        poller = self.service.poller
        self.store.add_job(Job(id="j1", host_id="h", state=STATE_RUNNING, submitted_at=1.0))
        poller.prime("h")
        poller._kickoff_until = 0.0  # as it is once the window has passed

        with patch.object(poller, "tick"):
            poller._on_kickoff()

        self.assertFalse(poller._kickoff.isActive())
        self.assertFalse(poller._primed)


class TestTheHostAnInputAlreadyLivesOn(DialogTestCase):
    """A file written into a host's mirror opens the wizard on that host."""

    def test_the_mirror_owner_is_found(self):
        mirror = os.path.join(self.tmp, "share")
        os.makedirs(mirror, exist_ok=True)
        host = make_host(id="mirror_host", name="mirrored", equal_path=mirror)
        self.store.add_host(host)
        self.store.add_host(make_host(id="plain_host", name="other"))

        found = self.store.host_for_local_path(os.path.join(mirror, "a", "mol.inp"))

        self.assertIsNotNone(found)
        self.assertEqual(found.id, host.id)

    def test_a_file_elsewhere_matches_nothing(self):
        host = make_host(equal_path=os.path.join(self.tmp, "share"))
        self.store.add_host(host)
        self.assertIsNone(self.store.host_for_local_path(os.path.join(self.tmp, "elsewhere.inp")))

    def test_the_most_specific_mirror_wins(self):
        outer = os.path.join(self.tmp, "mnt")
        inner = os.path.join(outer, "hpc")
        os.makedirs(inner, exist_ok=True)
        self.store.add_host(make_host(id="outer_host", name="outer", equal_path=outer))
        inner_host = make_host(id="inner_host", name="inner", equal_path=inner)
        self.store.add_host(inner_host)

        found = self.store.host_for_local_path(os.path.join(inner, "mol.inp"))

        self.assertEqual(found.id, inner_host.id)

    def test_a_disabled_host_is_not_offered(self):
        mirror = os.path.join(self.tmp, "share")
        os.makedirs(mirror, exist_ok=True)
        self.store.add_host(make_host(equal_path=mirror, enabled=False))
        self.assertIsNone(self.store.host_for_local_path(os.path.join(mirror, "mol.inp")))

    def test_the_wizard_opens_on_it(self):
        from job_manager.submit_dialog import SubmitDialog

        mirror = os.path.join(self.tmp, "share")
        os.makedirs(mirror, exist_ok=True)
        self.store.add_host(make_host(id="first_host", name="aaa_first_alphabetically"))
        owner = make_host(id="owner_host", name="zzz_mirrored", equal_path=mirror)
        self.store.add_host(owner)
        path = os.path.join(mirror, "mol.inp")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x\n")

        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.deleteLater)
        dialog.prefill(files=[path])

        self.assertEqual(dialog.current_host().id, owner.id)


if __name__ == "__main__":
    unittest.main()
