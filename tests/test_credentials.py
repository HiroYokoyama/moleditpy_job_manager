"""The password prompt, and the promise that no secret is ever written down.

``ask_password`` was stored on the host profile, shown in the Hosts dialog and
covered by service tests, but nothing in the plugin ever called
``set_password``: a password-only host could never authenticate, because the
paramiko backend was always handed ``None``.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager import credentials  # noqa: E402
from job_manager.models import (  # noqa: E402
    BACKEND_OPENSSH,
    BACKEND_PARAMIKO,
    HostProfile,
    SubmitPreset,
)
from job_manager.service import JobService  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

from .fakes import make_preset  # noqa: E402


class CredentialsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="credentials_")
        self.store = JobStore(self.tmp)
        self.host = HostProfile(name="c", hostname="h", backend=BACKEND_PARAMIKO, ask_password=True)
        self.store.add_host(self.host)
        self.service = JobService(self.store)
        self.addCleanup(self.service.shutdown)


class TestWhenToPrompt(CredentialsTestCase):
    def test_a_paramiko_host_that_asks_needs_one(self):
        self.assertTrue(credentials.needs_password(self.service, self.host))

    def test_not_once_it_has_been_answered(self):
        self.service.set_password(self.host.id, "already-known")
        self.assertFalse(credentials.needs_password(self.service, self.host))

    def test_openssh_hosts_are_never_prompted(self):
        host = HostProfile(name="o", hostname="h", backend=BACKEND_OPENSSH, ask_password=True)
        self.assertFalse(credentials.needs_password(self.service, host))

    def test_a_paramiko_host_that_uses_keys_is_not_prompted(self):
        host = HostProfile(name="k", hostname="h", backend=BACKEND_PARAMIKO, ask_password=False)
        self.assertFalse(credentials.needs_password(self.service, host))


class TestThePrompt(CredentialsTestCase):
    def test_the_answer_reaches_the_transport(self):
        with patch.object(credentials.QInputDialog, "getText", return_value=("s3cret", True)):
            self.assertTrue(credentials.ensure_password(self.service, self.host))
        self.assertTrue(self.service.has_password(self.host.id))

    def test_cancelling_stops_the_operation(self):
        with patch.object(credentials.QInputDialog, "getText", return_value=("", False)):
            self.assertFalse(credentials.ensure_password(self.service, self.host))
        self.assertFalse(self.service.has_password(self.host.id))

    def test_it_is_not_repeated_once_answered(self):
        self.service.set_password(self.host.id, "known")
        with patch.object(credentials.QInputDialog, "getText") as prompt:
            self.assertTrue(credentials.ensure_password(self.service, self.host))
        prompt.assert_not_called()

    def test_the_field_is_masked(self):
        from PyQt6.QtWidgets import QLineEdit

        with patch.object(credentials.QInputDialog, "getText", return_value=("x", True)) as prompt:
            credentials.ensure_password(self.service, self.host)
        self.assertIn(QLineEdit.EchoMode.Password, prompt.call_args[0])


class TestNoSecretIsPersisted(CredentialsTestCase):
    def test_the_password_is_not_in_settings_json(self):
        self.service.set_password(self.host.id, "hunter2")
        self.store.save_settings()
        with open(os.path.join(self.tmp, "settings.json"), encoding="utf-8") as handle:
            written = handle.read()
        self.assertNotIn("hunter2", written)

    def test_the_host_profile_has_no_password_field(self):
        self.service.set_password(self.host.id, "hunter2")
        self.assertNotIn("hunter2", json.dumps(self.host.to_dict()))

    def test_the_password_is_not_in_the_job_record(self):
        self.service.set_password(self.host.id, "hunter2")
        job = self.service.submit(self.host, make_preset(), "j", [__file__])
        self.assertNotIn("hunter2", json.dumps(job.to_dict()))

    def test_a_reloaded_store_has_no_memory_of_it(self):
        self.service.set_password(self.host.id, "hunter2")
        self.store.save_settings()
        self.assertFalse(JobService(JobStore(self.tmp)).has_password(self.host.id))


class TestForgettingAPassword(CredentialsTestCase):
    def test_setting_an_empty_password_clears_it(self):
        self.service.set_password(self.host.id, "x")
        self.service.set_password(self.host.id, "")
        self.assertFalse(self.service.has_password(self.host.id))

    def test_removing_a_host_forgets_its_password(self):
        from PyQt6.QtWidgets import QMessageBox

        from job_manager.hosts_dialog import HostsDialog

        self.service.set_password(self.host.id, "s3cret")
        dialog = HostsDialog(self.service)
        self.addCleanup(dialog.close)
        with patch(
            "job_manager.hosts_dialog.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog._remove_host()
        self.assertFalse(self.service.has_password(self.host.id))


class TestTheEntryPointsPromptFirst(CredentialsTestCase):
    """A worker thread cannot open a dialog, so the prompt has to come first."""

    def test_submitting_prompts_before_dispatching(self):
        from job_manager.submit_dialog import SubmitDialog

        self.store.add_preset(SubmitPreset(host_id=self.host.id, name="p"))
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        dialog.prefill(files=[os.path.abspath(__file__)], name="probe", host_id=self.host.id)

        submitted = []
        self.service.submit = lambda *a, **k: submitted.append(a)
        with patch("job_manager.submit_dialog.ensure_password", return_value=False) as prompt:
            dialog._submit()
        prompt.assert_called_once()
        self.assertEqual(submitted, [], "dispatched despite a cancelled prompt")

    def test_testing_a_connection_prompts_first(self):
        from job_manager.hosts_dialog import HostsDialog

        dialog = HostsDialog(self.service)
        self.addCleanup(dialog.close)
        with patch("job_manager.hosts_dialog.ensure_password", return_value=False) as prompt:
            dialog._test_connection()
        prompt.assert_called_once()
        self.assertEqual(dialog.lbl_test.text(), "Cancelled.")


if __name__ == "__main__":
    unittest.main()
