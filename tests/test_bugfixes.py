"""Regressions for three bugs found by review, all reproduced before fixing.

1. A job the scheduler kills reached the EXIT trap with ``$?`` still 0, so the
   wrapper wrote ``0`` to the sentinel and the plugin reported DONE, rc=0 for a
   calculation that never finished. Verified against a real bash below.
2. The monitor never disconnected from the service it outlives, so every
   open/close cycle left a live subscriber: a finished job's results were opened
   once per window the user had ever opened.
3. Resubmit prefilled a host_id the combo no longer contained, silently landing
   on whichever host sorted first.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from job_manager.models import SENTINEL_NAME, SubmitPreset
from job_manager.schedulers import get_scheduler

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI installs pytest only
    QApplication = None

BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "no bash available")
class TestKilledJobIsNotReportedDone(unittest.TestCase):
    """The sentinel must not read 0 for a job that was killed mid-run."""

    def _write_script(self, payload="sleep 30"):
        workdir = tempfile.mkdtemp(prefix="killed_job_")
        script = get_scheduler("shell").build_script(
            "t", SubmitPreset(command_template=payload), "mol.inp", "job.log"
        )
        path = os.path.join(workdir, "run.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        return workdir, path

    def _sentinel(self, workdir, timeout=8.0):
        target = os.path.join(workdir, SENTINEL_NAME)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(target) and os.path.getsize(target):
                with open(target, encoding="utf-8") as handle:
                    return handle.read().strip()
            time.sleep(0.1)
        return ""

    def test_sigterm_is_not_recorded_as_success(self):
        # The payload signals the wrapper itself rather than relying on
        # Popen.terminate(), which on Windows is a hard TerminateProcess that
        # no shell trap can observe -- there the test would pass vacuously.
        workdir, path = self._write_script(payload="kill -TERM $$; sleep 30")
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        recorded = self._sentinel(workdir)
        # The exact code may be 143 (trap ran) or absent (wrapper died first,
        # which the poller reports as LOST). Never 0.
        self.assertNotEqual(recorded, "0", "a killed job was recorded as a clean success")
        if recorded:
            self.assertEqual(recorded, "143")

    def test_the_traps_are_in_the_generated_script(self):
        script = get_scheduler("shell").build_script(
            "t", SubmitPreset(command_template="true"), "mol.inp", "job.log"
        )
        for line in ("trap 'exit 143' TERM", "trap 'exit 130' INT", "trap 'exit 129' HUP"):
            self.assertIn(line, script)

    def test_every_scheduler_carries_them(self):
        for name in ("slurm", "pbs", "sge", "shell"):
            script = get_scheduler(name).build_script(
                "t", SubmitPreset(command_template="true"), "mol.inp", "job.log"
            )
            self.assertIn("trap 'exit 143' TERM", script, name)

    def test_a_normal_exit_still_records_its_own_code(self):
        workdir, path = self._write_script(payload="exit 7")
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        self.assertEqual(self._sentinel(workdir), "7")

    def test_success_still_records_zero(self):
        workdir, path = self._write_script(payload="true")
        subprocess.run([BASH, path], capture_output=True, timeout=30)
        self.assertEqual(self._sentinel(workdir), "0")


@unittest.skipUnless(QApplication is not None, "PyQt6 not installed")
class TestMonitorReleasesTheServiceOnClose(unittest.TestCase):
    def setUp(self):
        from job_manager.service import JobService

        self.service = JobService(store=_temp_store())
        self.addCleanup(self.service.shutdown)

    def _dialog(self):
        from job_manager.jobs_dialog import JobsDialog

        return JobsDialog(self.service)

    def test_closing_drops_every_connection(self):
        before = self.service.receivers(self.service.jobs_changed)
        dialog = self._dialog()
        self.assertGreater(self.service.receivers(self.service.jobs_changed), before)
        dialog.close()
        self.assertEqual(self.service.receivers(self.service.jobs_changed), before)

    def test_results_are_opened_once_not_once_per_window_ever_opened(self):
        from job_manager import jobs_dialog as module

        opened = []
        original = module.open_in_host
        module.open_in_host = lambda path: bool(opened.append(path)) or True
        self.addCleanup(setattr, module, "open_in_host", original)

        for _ in range(3):
            self._dialog().close()
        live = self._dialog()
        self.addCleanup(live.close)

        self.service.results_ready.emit("job1", ["/tmp/result.out"])
        self.assertEqual(len(opened), 1, f"opened {len(opened)} times: {opened}")

    def test_a_closed_window_stops_reloading_its_model(self):
        dialog = self._dialog()
        reloads = []
        dialog.model.reload = lambda: reloads.append(1)
        dialog.close()
        self.service.jobs_changed.emit()
        self.assertEqual(reloads, [])

    def test_disconnecting_twice_is_harmless(self):
        dialog = self._dialog()
        dialog.close()
        dialog.close()  # a second closeEvent must not raise


@unittest.skipUnless(QApplication is not None, "PyQt6 not installed")
class TestResubmitWithADeletedHost(unittest.TestCase):
    def setUp(self):
        from job_manager.models import HostProfile, Job
        from job_manager.service import JobService

        self.service = JobService(store=_temp_store())
        self.addCleanup(self.service.shutdown)
        self.kept = HostProfile(name="aaa-other", hostname="other.example.org")
        self.service.store.add_host(self.kept)
        self.job = Job(name="orphan", host_id="gone-host-id", host_name="retired cluster")
        self.job.input_files = [__file__]
        self.service.store.add_job(self.job)

    def test_the_user_is_asked_before_landing_on_another_host(self):
        from PyQt6.QtWidgets import QMessageBox

        from job_manager import jobs_dialog as module
        from job_manager.jobs_dialog import JobsDialog

        dialog = JobsDialog(self.service)
        self.addCleanup(dialog.close)
        dialog.table.selectRow(dialog.model.row_of(self.job.id))

        asked = []
        original_question = module.QMessageBox.question
        module.QMessageBox.question = lambda *a, **k: (
            asked.append(a[2] if len(a) > 2 else ""),
            QMessageBox.StandardButton.No,
        )[1]
        self.addCleanup(setattr, module.QMessageBox, "question", original_question)

        opened = []
        dialog.open_submit_dialog = lambda **kwargs: opened.append(kwargs)
        dialog._resubmit_selected()

        self.assertEqual(len(asked), 1, "no warning shown for a deleted host")
        self.assertIn("no longer exists", asked[0])
        self.assertEqual(opened, [], "declining still opened the wizard")


def _temp_store():
    from job_manager.store import JobStore

    return JobStore(tempfile.mkdtemp(prefix="job_manager_bugfix_"))


if __name__ == "__main__":
    unittest.main()
