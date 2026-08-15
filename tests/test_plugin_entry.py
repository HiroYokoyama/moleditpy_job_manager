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
        paths = [call.args[0] for call in self.context.add_menu_action.call_args_list]
        self.assertEqual(
            paths,
            ["Extensions/Job Manager/Job Monitor", "Extensions/Job Manager/Submit Job..."],
        )

    def test_it_does_not_land_in_the_plugin_menu(self):
        # add_plugin_menu is hard-wired to "Plugin/<path>", which is the one
        # place these entries are not meant to be.
        job_manager.initialize(self.context)
        self.context.add_plugin_menu.assert_not_called()

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

    def test_creates_independent_window_with_no_parent(self):
        job_manager.initialize(self.context)
        with patch("job_manager.jobs_dialog.JobsDialog") as dialog_cls:
            job_manager.show_monitor(self.context)
        dialog_cls.assert_called_once()
        self.assertIsNone(dialog_cls.call_args.kwargs.get("parent"))

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
        # PLUGIN_DEVELOPMENT_MANUAL_V4.md 3.1: "Add item to any main menu".
        # The host creates a top-level menu it does not have, which is how
        # these entries reach Extensions rather than Plugin.
        "add_menu_action",
        "get_window",
        "register_window",
        "get_main_window",
        "show_status_message",
        # PLUGIN_DEVELOPMENT_MANUAL_V4.md 2.2: makes .pmejbs a file type the
        # application knows (File > Import, the command line, and drops).
        "register_file_opener",
        # PLUGIN_DEVELOPMENT_MANUAL_V4.md: a drop on the main window. Answers
        # for .pmejbs and declines everything else.
        "register_drop_handler",
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


class TestTheDropHandler(PluginEntryTestCase):
    """A job list dropped on the main window opens in the monitor."""

    def test_it_is_registered(self):
        job_manager.initialize(self.context)
        self.context.register_drop_handler.assert_called_once()
        callback, priority = self.context.register_drop_handler.call_args.args
        self.assertIs(callback, job_manager.handle_dropped_file)
        self.assertEqual(priority, 0)

    def test_a_job_list_is_taken(self):
        with patch.object(job_manager, "open_job_file") as opened:
            self.assertTrue(job_manager.handle_dropped_file("/tmp/jobs.pmejbs"))
        opened.assert_called_once_with("/tmp/jobs.pmejbs")

    def test_the_extension_is_matched_whatever_its_case(self):
        with patch.object(job_manager, "open_job_file"):
            self.assertTrue(job_manager.handle_dropped_file("/tmp/JOBS.PMEJBS"))

    def test_an_input_file_is_left_to_the_application(self):
        # Claiming .inp and .xyz would stop a drop on the main window doing the
        # obvious thing, which is opening the molecule. The monitor and the
        # wizard accept those themselves.
        for path in ("/tmp/mol.inp", "/tmp/mol.xyz", "/tmp/notes.txt", ""):
            with patch.object(job_manager, "open_job_file") as opened:
                self.assertFalse(job_manager.handle_dropped_file(path), path)
            opened.assert_not_called()

    def test_a_file_that_will_not_open_is_declined_rather_than_raising(self):
        # Returning True for a file it could not open would have the host stop
        # offering it to anything else.
        with patch.object(job_manager, "open_job_file", side_effect=RuntimeError("bad file")):
            self.assertFalse(job_manager.handle_dropped_file("/tmp/broken.pmejbs"))
