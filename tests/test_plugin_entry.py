"""initialize()/run() against a mock PluginContext, plus the real contract."""

import importlib
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

import job_manager  # noqa: E402


class PluginEntryTestCase(unittest.TestCase):
    def setUp(self):
        importlib.reload(job_manager)
        self.context = MagicMock()
        self.context.get_window.return_value = None
        self.addCleanup(job_manager.shutdown)

    def tearDown(self):
        job_manager._context = None
        job_manager._service = None


class TestInitialize(PluginEntryTestCase):
    def test_registers_both_menu_entries(self):
        job_manager.initialize(self.context)
        paths = [call.args[0] for call in self.context.add_plugin_menu.call_args_list]
        self.assertEqual(paths, ["Job Manager/Job Monitor", "Job Manager/Submit Job..."])

    def test_the_context_is_remembered(self):
        job_manager.initialize(self.context)
        self.assertIs(job_manager.get_context(), self.context)

    def test_no_service_is_built_for_an_empty_job_list(self):
        # A list with active jobs in it does start one, so that a restart keeps
        # tracking them; see tests/test_background_tracking.py.
        job_manager.initialize(self.context)
        self.assertIsNone(job_manager.get_service(create=False))

    def test_no_network_activity_at_load_with_nothing_to_track(self):
        with patch("job_manager.service.JobService") as service_cls:
            job_manager.initialize(self.context)
        service_cls.assert_not_called()


class TestShowMonitor(PluginEntryTestCase):
    def test_creates_and_registers_the_window(self):
        job_manager.initialize(self.context)
        with patch("job_manager.jobs_dialog.JobsDialog") as dialog_cls:
            job_manager.show_monitor(self.context)
        self.context.register_window.assert_called_once()
        self.assertEqual(self.context.register_window.call_args.args[0], "job_monitor")
        dialog_cls.return_value.show.assert_called_once()

    def test_an_existing_window_is_raised_not_rebuilt(self):
        job_manager.initialize(self.context)
        existing = MagicMock()
        self.context.get_window.return_value = existing
        with patch("job_manager.jobs_dialog.JobsDialog") as dialog_cls:
            job_manager.show_monitor(self.context)
        dialog_cls.assert_not_called()
        existing.raise_.assert_called_once()

    def test_a_construction_failure_is_reported_not_raised(self):
        job_manager.initialize(self.context)
        with patch("job_manager.jobs_dialog.JobsDialog", side_effect=RuntimeError("boom")):
            job_manager.show_monitor(self.context)
        self.context.show_status_message.assert_called_once()

    def test_without_a_context_it_is_a_no_op(self):
        job_manager.show_monitor(None)


class TestShowSubmit(PluginEntryTestCase):
    def test_opens_the_monitor_then_the_wizard(self):
        job_manager.initialize(self.context)
        window = MagicMock()
        self.context.get_window.side_effect = [None, window]
        with patch("job_manager.jobs_dialog.JobsDialog", return_value=window):
            job_manager.show_submit(self.context)
        window.open_submit_dialog.assert_called_once()

    def test_without_a_context_it_is_a_no_op(self):
        job_manager.show_submit(None)


class TestForgetWindow(PluginEntryTestCase):
    def test_deregisters_so_a_reopen_is_a_fresh_window(self):
        job_manager.initialize(self.context)
        job_manager.forget_window()
        self.context.register_window.assert_called_with("job_monitor", None)

    def test_a_failing_host_does_not_propagate(self):
        job_manager.initialize(self.context)
        self.context.register_window.side_effect = RuntimeError("gone")
        job_manager.forget_window()

    def test_without_a_context_it_is_a_no_op(self):
        job_manager.forget_window()


class TestLegacyRun(PluginEntryTestCase):
    def test_run_opens_the_monitor(self):
        job_manager.initialize(self.context)
        with patch("job_manager.show_monitor") as show:
            job_manager.run(MagicMock())
        show.assert_called_once()


class TestServiceLifecycle(PluginEntryTestCase):
    def test_the_service_is_a_singleton(self):
        first = job_manager.get_service()
        self.assertIs(job_manager.get_service(), first)

    def test_shutdown_releases_it(self):
        job_manager.get_service()
        job_manager.shutdown()
        self.assertIsNone(job_manager.get_service(create=False))

    def test_shutdown_without_a_service_is_safe(self):
        job_manager.shutdown()

    def test_a_failing_shutdown_is_contained(self):
        service = job_manager.get_service()
        with patch.object(service, "shutdown", side_effect=RuntimeError("stuck")):
            job_manager.shutdown()
        self.assertIsNone(job_manager.get_service(create=False))


class TestContextContract(unittest.TestCase):
    """Only PluginContext methods the manual documents may be used."""

    DOCUMENTED = {
        "add_plugin_menu",
        "get_window",
        "register_window",
        "get_main_window",
        "show_status_message",
        # PLUGIN_DEVELOPMENT_MANUAL_V4.md 2.2: makes .pmejbs a file type the
        # application knows (File > Import, the command line, and drops).
        "register_file_opener",
    }

    def test_only_documented_context_methods_are_called(self):
        importlib.reload(job_manager)
        context = MagicMock()
        context.get_window.return_value = None
        job_manager.initialize(context)
        job_manager.forget_window()
        used = {name for name, *_ in context.method_calls}
        self.assertTrue(used.issubset(self.DOCUMENTED), used - self.DOCUMENTED)


class TestSubmitFilePublicAPI(PluginEntryTestCase):
    """The contract input-generator plugins call. Renaming breaks them."""

    def test_the_function_exists_with_the_documented_name(self):
        self.assertTrue(callable(job_manager.submit_file))

    def test_a_single_path_is_accepted(self):
        job_manager.initialize(self.context)
        window = MagicMock()
        self.context.get_window.side_effect = [None, window, window]
        with patch("job_manager.jobs_dialog.JobsDialog", return_value=window):
            self.assertTrue(job_manager.submit_file("/tmp/mol.inp"))
        window.open_submit_dialog.assert_called_once_with(files=["/tmp/mol.inp"], name="")

    def test_a_list_of_paths_is_accepted(self):
        job_manager.initialize(self.context)
        window = MagicMock()
        self.context.get_window.side_effect = [None, window, window]
        with patch("job_manager.jobs_dialog.JobsDialog", return_value=window):
            job_manager.submit_file(["/tmp/a.inp", "/tmp/b.xyz"], name="run")
        window.open_submit_dialog.assert_called_once_with(
            files=["/tmp/a.inp", "/tmp/b.xyz"], name="run"
        )

    def test_no_paths_returns_false(self):
        job_manager.initialize(self.context)
        self.assertFalse(job_manager.submit_file([]))
        self.assertFalse(job_manager.submit_file(""))

    def test_blank_entries_are_dropped(self):
        job_manager.initialize(self.context)
        self.assertFalse(job_manager.submit_file(["", None]))

    def test_without_a_context_it_returns_false_instead_of_raising(self):
        # A generator may call this before the host has initialised us.
        job_manager._context = None
        self.assertFalse(job_manager.submit_file("/tmp/mol.inp"))

    def test_a_failure_in_the_wizard_is_contained(self):
        job_manager.initialize(self.context)
        window = MagicMock()
        window.open_submit_dialog.side_effect = RuntimeError("boom")
        self.context.get_window.side_effect = [None, window, window]
        with patch("job_manager.jobs_dialog.JobsDialog", return_value=window):
            self.assertFalse(job_manager.submit_file("/tmp/mol.inp"))


if __name__ == "__main__":
    unittest.main()
