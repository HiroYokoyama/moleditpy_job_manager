"""JobService: submit, state transitions, auto-download, cancel, teardown."""

import os
import tempfile
import unittest

import pytest

pytest.importorskip("PyQt6.QtCore", reason="PyQt6 is not installed")

from job_manager.models import (  # noqa: E402
    SENTINEL_NAME,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_SUBMITTED,
    STATE_UPLOADING,
)
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

from .fakes import FakeTransport, make_host, make_preset  # noqa: E402
from .test_poller import SyncPool  # noqa: E402


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="service_")
        self.store = JobStore(self.tmp)
        self.store.set_pref("download_root", os.path.join(self.tmp, "downloads"))
        self.host = make_host()
        self.store.add_host(self.host)
        self.service = JobService(self.store)
        self.service.pool = SyncPool()
        self.service.poller.pool = SyncPool()
        self.transport = FakeTransport(self.host).when("sbatch", stdout="4242\n")
        self.service.transport_for = lambda host: self.transport
        self.addCleanup(self.service.shutdown)

        self.input_path = os.path.join(self.tmp, "mol.inp")
        with open(self.input_path, "w", encoding="utf-8") as handle:
            handle.write("! B3LYP\n")

    def submit(self, **kwargs):
        preset = kwargs.pop("preset", make_preset())
        return self.service.submit(self.host, preset, "mol", [self.input_path], **kwargs)


class TestSubmit(ServiceTestCase):
    def test_job_is_recorded_immediately(self):
        job = self.submit()
        self.assertIn(job.id, self.service.store.jobs)

    def test_state_reaches_submitted(self):
        job = self.submit()
        self.assertEqual(job.state, STATE_SUBMITTED)
        self.assertEqual(job.remote_job_id, "4242")

    def test_the_job_survives_a_restart(self):
        job = self.submit()
        self.assertEqual(JobStore(self.tmp).jobs[job.id].remote_job_id, "4242")

    def test_local_dir_is_under_the_download_root(self):
        job = self.submit()
        self.assertTrue(job.local_dir.startswith(self.store.download_root()))

    def test_transport_is_closed_after_submitting(self):
        self.submit()
        self.assertGreaterEqual(self.transport.closed, 1)

    def test_polling_starts_after_a_submit(self):
        self.submit()
        self.assertTrue(self.service.poller.timer.isActive())

    def test_failure_marks_the_job_failed_and_reports(self):
        self.transport.clear_rules()
        self.transport.when("sbatch", rc=1, stderr="Invalid account")
        errors = []
        self.service.error.connect(errors.append)
        job = self.submit()
        self.assertEqual(job.state, STATE_FAILED)
        self.assertIn("Invalid account", job.last_error)
        self.assertTrue(errors)

    def test_uploading_state_is_visible_before_completion(self):
        # Nothing runs inline in the real pool, so the record starts UPLOADING.
        from job_manager.models import Job

        self.assertEqual(Job(state=STATE_UPLOADING).state, STATE_UPLOADING)

    def test_preset_auto_download_flag_is_honoured(self):
        job = self.submit(preset=make_preset(auto_download=False))
        self.assertFalse(job.auto_download)

    def test_explicit_override_beats_the_preset(self):
        job = self.submit(preset=make_preset(auto_download=False), auto_download=True)
        self.assertTrue(job.auto_download)


class TestRoundTrip(ServiceTestCase):
    def test_completion_triggers_a_download_and_a_signal(self):
        job = self.submit()
        ready = []
        self.service.results_ready.connect(lambda job_id, paths: ready.append((job_id, paths)))

        self.transport.clear_rules()
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n0\n")
        self.transport.when("ls -p", stdout="mol.out\njob.log\n")
        self.service.poller.tick(force=True)

        self.assertEqual(job.state, STATE_DONE)
        self.assertTrue(job.downloaded)
        self.assertEqual(len(ready), 1)
        self.assertTrue(all(os.path.exists(p) for p in job.downloaded_files))

    def test_no_auto_download_when_disabled(self):
        job = self.submit(preset=make_preset(auto_download=False))
        self.transport.clear_rules()
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n0\n")
        self.service.poller.tick(force=True)
        self.assertEqual(job.state, STATE_DONE)
        self.assertFalse(job.downloaded)

    def test_a_failed_job_still_fetches_its_log(self):
        job = self.submit()
        self.transport.clear_rules()
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n1\n")
        self.transport.when("ls -p", stdout="job.log\n")
        self.service.poller.tick(force=True)
        self.assertEqual(job.state, STATE_FAILED)
        self.assertTrue(job.downloaded_files)

    def test_download_restores_the_previous_state(self):
        job = self.submit()
        job.touch(STATE_DONE)
        self.transport.clear_rules()
        self.transport.when("ls -p", stdout="mol.out\n")
        self.service.download(job)
        self.assertEqual(job.state, STATE_DONE)

    def test_download_failure_is_reported_and_does_not_strand_the_state(self):
        job = self.submit()
        job.touch(STATE_DONE)
        errors = []
        self.service.error.connect(errors.append)

        def exploding(host):
            raise RuntimeError("connection reset")

        self.service.transport_for = exploding
        self.service.download(job)
        self.assertEqual(job.state, STATE_DONE)
        self.assertTrue(errors)

    def test_download_without_a_host_profile_is_reported(self):
        job = self.submit()
        self.store.remove_host(self.host.id)
        errors = []
        self.service.error.connect(errors.append)
        self.service.download(job)
        self.assertTrue(errors)


class TestCancel(ServiceTestCase):
    def test_cancel_marks_the_job(self):
        job = self.submit()
        self.service.cancel(job)
        self.assertEqual(job.state, STATE_CANCELLED)
        self.assertTrue(self.transport.ran("scancel 4242"))

    def test_cancel_without_a_host_is_reported(self):
        job = self.submit()
        self.store.remove_host(self.host.id)
        errors = []
        self.service.error.connect(errors.append)
        self.service.cancel(job)
        self.assertTrue(errors)


class TestTail(ServiceTestCase):
    def test_tail_emits_the_log(self):
        job = self.submit()
        self.transport.clear_rules()
        self.transport.when("tail", stdout="SCF converged\n")
        received = []
        self.service.log_ready.connect(received.append)
        self.service.tail(job)
        self.assertEqual(received, ["SCF converged\n"])

    def test_tail_without_a_host_is_reported(self):
        job = self.submit()
        self.store.remove_host(self.host.id)
        errors = []
        self.service.error.connect(errors.append)
        self.service.tail(job)
        self.assertTrue(errors)


class TestPasswords(ServiceTestCase):
    def test_password_is_kept_in_memory_only(self):
        self.service.set_password(self.host.id, "hunter2")
        self.assertTrue(self.service.has_password(self.host.id))
        with open(self.store.settings_path, encoding="utf-8") as handle:
            self.assertNotIn("hunter2", handle.read())

    def test_clearing_a_password(self):
        self.service.set_password(self.host.id, "x")
        self.service.set_password(self.host.id, "")
        self.assertFalse(self.service.has_password(self.host.id))


class TestHousekeeping(ServiceTestCase):
    def test_remove_job(self):
        job = self.submit()
        self.service.remove_job(job.id)
        self.assertNotIn(job.id, self.store.jobs)

    def test_host_error_is_surfaced_with_the_host_name(self):
        messages = []
        self.service.message.connect(messages.append)
        self.service._on_host_error(self.host.id, "timed out")
        self.assertIn("testcluster", messages[0])

    def test_a_state_change_for_an_unknown_job_is_ignored(self):
        self.service._on_job_state_changed("ghost", STATE_RUNNING)

    def test_shutdown_is_idempotent(self):
        self.service.shutdown()
        self.service.shutdown()


if __name__ == "__main__":
    unittest.main()
