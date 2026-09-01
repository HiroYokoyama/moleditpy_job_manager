"""The plugin's side of the API: the switch, the lifetime, and `submit_job()`.

The security posture is a property of *this* file's behaviour, not only of the
server's: a plugin that starts listening because it was installed would be a
different plugin, so "off unless the preference says otherwise" is asserted
here against the real entry point.
"""

import importlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

import job_manager  # noqa: E402
from job_manager.api_core import ApiError  # noqa: E402
from job_manager.models import HostProfile, Job  # noqa: E402
from job_manager.store import JobStore  # noqa: E402


class ApiEntryTestCase(unittest.TestCase):
    """A private data directory: the plugin reads one at load."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="api_entry_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._previous_dir = os.environ.get("MOLEDITPY_JOB_MANAGER_DIR")
        os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = self.tmp
        self.addCleanup(self._restore)
        importlib.reload(job_manager)
        self.addCleanup(job_manager.shutdown)
        self.context = MagicMock()
        self.context.get_window.return_value = None

    def _restore(self):
        if self._previous_dir is None:
            os.environ.pop("MOLEDITPY_JOB_MANAGER_DIR", None)
        else:
            os.environ["MOLEDITPY_JOB_MANAGER_DIR"] = self._previous_dir
        job_manager._context = None
        job_manager._service = None
        job_manager._status_widget = None
        job_manager._api_server = None

    def enable_api(self):
        store = JobStore(self.tmp)
        store.set_pref("api_enabled", True)
        store.set_pref("api_port", 0)

    def make_host(self):
        service = job_manager.get_service()
        return service.store.add_host(HostProfile(name="mycluster", scheduler="slurm"))


class TestTheSwitch(ApiEntryTestCase):
    def test_loading_the_plugin_starts_nothing(self):
        # The whole security posture: a machine where this is merely installed
        # has no socket open and no token on disk.
        job_manager.initialize(self.context)
        self.assertFalse(job_manager.api_is_running())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "api.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "api_token")))

    def test_the_preference_being_on_starts_it_at_load(self):
        self.enable_api()
        job_manager.initialize(self.context)
        self.assertTrue(job_manager.api_is_running())
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "api.json")))

    def test_an_unreadable_job_list_does_not_start_the_api_or_raise(self):
        with open(os.path.join(self.tmp, "jobs.pmejbs"), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        job_manager.initialize(self.context)
        self.assertFalse(job_manager.api_is_running())

    def test_start_and_stop_are_idempotent(self):
        job_manager.initialize(self.context)
        port = job_manager.start_api(0)
        self.assertTrue(port)
        self.assertEqual(job_manager.start_api(0), port)
        job_manager.stop_api()
        job_manager.stop_api()
        self.assertFalse(job_manager.api_is_running())

    def test_stopping_without_ever_starting_is_harmless(self):
        job_manager.stop_api()
        self.assertFalse(job_manager.api_is_running())

    def test_a_port_that_cannot_be_bound_is_reported_not_raised(self):
        job_manager.initialize(self.context)
        with patch("job_manager.api_server.JobApiServer.start", side_effect=OSError("no")):
            self.assertEqual(job_manager.start_api(0), 0)
        self.context.show_status_message.assert_called()

    def test_shutdown_closes_the_socket_and_removes_the_endpoint_file(self):
        # A listening socket that outlives the plugin would answer requests
        # with a service that has been torn down underneath it.
        self.enable_api()
        job_manager.initialize(self.context)
        self.assertTrue(job_manager.api_is_running())
        job_manager.shutdown()
        self.assertFalse(job_manager.api_is_running())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "api.json")))

    def test_the_startup_peek_reads_the_store_once_with_the_api_on(self):
        # Both resume paths take the same store; two would parse both files
        # twice at every launch and leave two views of the same jobs.
        self.enable_api()
        with patch("job_manager.store.JobStore.load", autospec=True) as load:
            job_manager.initialize(self.context)
        self.assertEqual(load.call_count, 1)


class TestSubmitJob(ApiEntryTestCase):
    """The in-process handoff, for a plugin that would otherwise use a socket."""

    def setUp(self):
        super().setUp()
        job_manager.initialize(self.context)
        self.host = self.make_host()
        handle, self.input_path = tempfile.mkstemp(suffix=".inp", dir=self.tmp)
        os.close(handle)

    def test_it_submits_without_opening_the_wizard(self):
        with patch.object(job_manager.get_service(), "submit") as submit:
            submit.return_value = Job(name="water", host_id=self.host.id, host_name=self.host.name)
            record = job_manager.submit_job(
                {
                    "host": "mycluster",
                    "files": [self.input_path],
                    "command": "mycommand {input}",
                    "name": "water",
                }
            )
        self.assertEqual(record["name"], "water")
        self.context.get_window.assert_not_called()

    def test_a_bad_request_raises_an_ApiError_with_a_readable_message(self):
        with self.assertRaises(ApiError) as caught:
            job_manager.submit_job({"host": "nowhere", "command": "x"})
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("mycluster", caught.exception.message)

    def test_it_is_the_same_handler_the_socket_serves(self):
        # Not a parallel implementation: the same JobApi.submit, so a rule
        # added for one caller cannot be missing for the other.
        with patch("job_manager.api_core.JobApi.submit") as submit:
            submit.return_value = {"job": {"id": "x"}}
            self.assertEqual(job_manager.submit_job({"host": "mycluster"}), {"id": "x"})
        submit.assert_called_once_with({"host": "mycluster"})


class TestTheApiDialog(ApiEntryTestCase):
    def setUp(self):
        super().setUp()
        job_manager.initialize(self.context)
        from job_manager.api_dialog import ApiDialog

        self.dialog = ApiDialog(job_manager.get_service())
        self.addCleanup(self.dialog.deleteLater)

    def test_it_opens_showing_the_api_as_off(self):
        self.assertFalse(self.dialog.chk_enabled.isChecked())
        self.assertEqual(self.dialog.txt_url.text(), "")
        self.assertIn("Not listening", self.dialog.lbl_status.text())

    def test_ticking_it_starts_the_server_and_remembers_the_choice(self):
        self.dialog.chk_enabled.setChecked(True)
        self.assertTrue(job_manager.api_is_running())
        self.assertTrue(job_manager.get_service().store.get_pref("api_enabled"))
        self.assertTrue(self.dialog.txt_url.text().startswith("http://127.0.0.1:"))
        self.assertTrue(self.dialog.txt_token.text())

    def test_unticking_it_stops_the_server_and_remembers_that_too(self):
        self.dialog.chk_enabled.setChecked(True)
        self.dialog.chk_enabled.setChecked(False)
        self.assertFalse(job_manager.api_is_running())
        self.assertFalse(job_manager.get_service().store.get_pref("api_enabled"))

    def test_the_token_is_masked_until_it_is_asked_for(self):
        from PyQt6.QtWidgets import QLineEdit

        self.assertEqual(self.dialog.txt_token.echoMode(), QLineEdit.EchoMode.Password)

    def test_changing_the_port_while_it_runs_rebinds_it(self):
        self.dialog.chk_enabled.setChecked(True)
        first = job_manager.get_api_server().port
        self.dialog.spin_port.setValue(first + 1 if first < 65535 else first - 1)
        self.assertTrue(job_manager.api_is_running())
        self.assertNotEqual(job_manager.get_api_server().port, first)

    def test_the_new_token_button_replaces_the_token_after_confirmation(self):
        from PyQt6.QtWidgets import QMessageBox

        self.dialog.chk_enabled.setChecked(True)
        before = self.dialog.txt_token.text()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog._renew()
        self.assertNotEqual(self.dialog.txt_token.text(), before)

    def test_declining_the_confirmation_keeps_the_token(self):
        from PyQt6.QtWidgets import QMessageBox

        self.dialog.chk_enabled.setChecked(True)
        before = self.dialog.txt_token.text()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.dialog._renew()
        self.assertEqual(self.dialog.txt_token.text(), before)


if __name__ == "__main__":
    unittest.main()
