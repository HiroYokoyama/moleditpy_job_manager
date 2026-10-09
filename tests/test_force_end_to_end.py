"""Force run, the queue listing and the host file calls, on a real shell.

The fake-transport tests prove what is sent; these prove it does the thing:
with the only slot taken by a job that will not end until told to, a forced
job really starts beside it, the helper really counts it and reaps it, and
the poll reads it DONE afterwards.
"""

from __future__ import annotations

import hashlib
import os
import time
import unittest

from job_manager.models import SCHEDULER_WINDOWS, Job
from job_manager.runner import (
    download_host_paths,
    force_in_runner,
    list_host_path,
    poll_runner,
    runner_queue,
    stat_host_path,
    submit_to_runner,
)

from .bash_support import bash_path
from .fakes import make_preset
from .test_runner_end_to_end import BASH, ON_WINDOWS, EndToEndCase

#: How long a job may take to start. Generous, because Windows PowerShell 5.1
#: under a full parallel suite has taken well over the harness's 15 s to start
#: one; nothing waits this out when it works.
START_TIMEOUT = 90.0


@unittest.skipUnless(BASH, "needs a bash")
class TestBashForce(EndToEndCase):
    def setUp(self):
        super().setUp()
        # One slot, so a second job has to wait -- unless it is forced.
        self.host.max_concurrent = 1
        self.stop = self.marker("STOP")
        # Registered after the base cleanup, so it runs first: the blocking
        # job is let go before its directory is taken away.
        self.addCleanup(self._release)

    def _release(self):
        open(self.stop, "a").close()

    def command_that_waits(self, path: str) -> str:
        return f"while [ ! -f {bash_path(path)} ]; do sleep 0.1; done"

    def submit_job(self, name, command, force=False) -> Job:
        job = Job(name=name, host_id=self.host.id, scheduler=self.scheduler)
        preset = make_preset(command_template=command)
        return submit_to_runner(self.transport(), self.host, preset, job, [self.input], force=force)

    def fill_the_only_slot(self) -> Job:
        ready = self.marker("BLOCKER_STARTED")
        blocker = self.submit_job(
            "long", self.command_that_touches(ready) + "\n" + self.command_that_waits(self.stop)
        )
        # A running/ entry precedes both shell launches; it does not prove
        # the payload has started occupying the slot yet.
        self.wait_for(lambda: os.path.exists(ready), timeout=START_TIMEOUT, what="the long payload")
        return blocker

    def test_releasing_the_blocker_finishes_its_wrapper_and_runner(self):
        blocker = self.fill_the_only_slot()
        self._release()
        self.wait_for(
            lambda: poll_runner(self.transport(), self.host, [blocker]).get(blocker.id) == "DONE",
            timeout=START_TIMEOUT,
            what="the released blocker to finish",
        )

    def test_a_waiting_job_forced_starts_beside_the_one_holding_the_slot(self):
        self.fill_the_only_slot()
        quick = self.submit_job("quick", self.command_that_touches(self.marker("QUICK")))
        time.sleep(1.0)
        self.assertFalse(os.path.exists(self.marker("QUICK")), "the slot limit did not hold")

        self.assertTrue(force_in_runner(self.transport(), self.host, quick))

        self.wait_for(
            lambda: os.path.exists(self.marker("QUICK")),
            timeout=START_TIMEOUT,
            what="the forced job",
        )
        self.assertFalse(os.path.exists(self.stop))

    def test_a_forced_submission_starts_at_once(self):
        self.fill_the_only_slot()
        quick = self.submit_job(
            "quick", self.command_that_touches(self.marker("QUICK")), force=True
        )

        self.wait_for(
            lambda: os.path.exists(self.marker("QUICK")),
            timeout=START_TIMEOUT,
            what="the forced job",
        )
        self.assertTrue(quick.force_run)

    def test_the_forced_job_is_reaped_and_polls_done(self):
        self.fill_the_only_slot()
        quick = self.submit_job(
            "quick", self.command_that_touches(self.marker("QUICK")), force=True
        )
        self.wait_for(
            lambda: poll_runner(self.transport(), self.host, [quick]).get(quick.id) == "DONE",
            timeout=START_TIMEOUT,
            what="the forced job to read DONE",
        )

    def test_forcing_a_job_that_already_started_changes_nothing(self):
        blocker = self.fill_the_only_slot()
        self.assertFalse(force_in_runner(self.transport(), self.host, blocker))

    def test_the_queue_listing_shows_what_runs_and_what_waits(self):
        blocker = self.fill_the_only_slot()
        quick = self.submit_job("quick", self.command_that_touches(self.marker("QUICK")))

        detail = runner_queue(self.transport(), self.host)

        self.assertEqual([item["job_id"] for item in detail["running"]], [blocker.id])
        self.assertEqual([item["job_id"] for item in detail["waiting"]], [quick.id])
        self.assertEqual(detail["limits"].get("slots"), 1)


@unittest.skipUnless(BASH, "needs a bash")
class TestBashHostFiles(EndToEndCase):
    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.root, "jobs", "data")
        os.makedirs(os.path.join(self.data, "sub"))
        self.payload = b"FINAL ENERGY -76.4\n" * 50
        with open(os.path.join(self.data, "result.out"), "wb") as handle:
            handle.write(self.payload)
        self.remote_data = f"{self.host.remote_root}/data"

    def test_stat_reports_the_real_size_and_sha256(self):
        info = stat_host_path(self.transport(), self.host, f"{self.remote_data}/result.out")
        self.assertEqual(info["type"], "file")
        self.assertEqual(info["size"], len(self.payload))
        self.assertEqual(info["sha256"], hashlib.sha256(self.payload).hexdigest())

    def test_stat_tells_a_directory_from_nothing(self):
        transport = self.transport()
        self.assertEqual(
            stat_host_path(transport, self.host, self.remote_data)["type"], "directory"
        )
        self.assertFalse(stat_host_path(transport, self.host, f"{self.remote_data}/no")["exists"])

    def test_listing_marks_directories(self):
        names = list_host_path(self.transport(), self.host, self.remote_data)
        self.assertIn("result.out", names)
        self.assertIn("sub/", names)

    def test_download_fetches_by_path_and_will_not_overwrite_unasked(self):
        into = os.path.join(self.root, "fetched")
        remote = f"{self.remote_data}/result.out"
        downloaded, skipped = download_host_paths(self.transport(), self.host, [remote], into)
        self.assertEqual(skipped, [])
        with open(downloaded[0], "rb") as handle:
            self.assertEqual(handle.read(), self.payload)

        again, skipped = download_host_paths(self.transport(), self.host, [remote], into)
        self.assertEqual(again, [])
        self.assertIn("already exists", skipped[0][1])

        again, skipped = download_host_paths(
            self.transport(), self.host, [remote], into, overwrite=True
        )
        self.assertEqual(len(again), 1)

    def test_a_missing_file_is_skipped_with_a_reason(self):
        into = os.path.join(self.root, "fetched")
        _, skipped = download_host_paths(
            self.transport(), self.host, [f"{self.remote_data}/nope.out"], into
        )
        self.assertIn("no such file", skipped[0][1])


@unittest.skipUnless(ON_WINDOWS, "the PowerShell runner needs Windows")
class TestPowerShellForce(TestBashForce):
    scheduler = SCHEDULER_WINDOWS

    def command_that_touches(self, path: str) -> str:
        return f"New-Item -ItemType File -Path '{path}' | Out-Null"

    def command_that_waits(self, path: str) -> str:
        return f"while (-not (Test-Path '{path}')) {{ Start-Sleep -Milliseconds 100 }}"


@unittest.skipUnless(ON_WINDOWS, "the PowerShell runner needs Windows")
class TestPowerShellHostFiles(TestBashHostFiles):
    scheduler = SCHEDULER_WINDOWS


if __name__ == "__main__":
    unittest.main()
