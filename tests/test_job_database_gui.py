"""Both portable formats and the explicit legacy Convert menu action."""

from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from job_manager.job_database import read_document  # noqa: E402
from job_manager.jobs_dialog import (  # noqa: E402
    DATABASE_FILTER,
    JOB_LIST_FILTER,
    LEGACY_LIST_FILTER,
    JobsDialog,
)
from job_manager.models import Job  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TestDatabaseMenus(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.store.add_job(Job(id="synthetic"))
        self.dialog = JobsDialog(self.service)
        self.addCleanup(self.dialog.close)
        self.addCleanup(self.dialog.deleteLater)

    def legacy(self):
        path = str(Path(self.tmp) / "legacy.pmejbs")
        self.store.export_jobs(path)
        self.store.use_jobs_file(path)
        self.dialog._update_actions()
        return Path(path)

    def test_convert_is_in_file_menu_and_enabled_only_for_legacy(self):
        action = self.dialog.job_actions["convert"]
        menu = self.dialog.menu_bar.actions()[0].menu()
        self.assertIn(action, menu.actions())
        self.assertFalse(action.isEnabled())
        source = self.legacy()
        self.assertTrue(action.isEnabled())
        original = source.read_bytes()
        with patch("job_manager.jobs_dialog.QMessageBox.question", return_value=QMessageBox.Yes):
            action.trigger()
        self.assertFalse(action.isEnabled())
        self.assertTrue(self.store.jobs_path.endswith(".pmejdb"))
        self.assertFalse(source.exists())
        self.assertEqual(Path(self.store.archived_files()[0]).read_bytes(), original)

    def test_declining_conversion_changes_nothing(self):
        source = self.legacy()
        before = source.read_bytes()
        with patch("job_manager.jobs_dialog.QMessageBox.question", return_value=QMessageBox.No):
            self.dialog.job_actions["convert"].trigger()
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(source.with_suffix(".pmejdb").exists())

    def test_conversion_failure_is_reported_in_gui(self):
        source = self.legacy()
        with (
            patch("job_manager.jobs_dialog.QMessageBox.question", return_value=QMessageBox.Yes),
            patch.object(self.store, "convert_jobs", side_effect=OSError("synthetic failure")),
            patch("job_manager.jobs_dialog.QMessageBox.warning") as warned,
        ):
            self.dialog.job_actions["convert"].trigger()
        warned.assert_called_once()
        self.assertTrue(source.exists())

    def test_archive_view_disables_conversion(self):
        self.legacy()
        self.dialog._show_archive("synthetic.pmejbs", list(self.store.jobs.values()))
        self.assertFalse(self.dialog.job_actions["convert"].isEnabled())

    def test_save_as_offers_both_formats_and_adds_selected_extension(self):
        for index, (selected, extension) in enumerate(
            ((DATABASE_FILTER, ".pmejdb"), (LEGACY_LIST_FILTER, ".pmejbs"))
        ):
            target = str(Path(self.tmp) / f"shared-{index}")
            with patch(
                "job_manager.jobs_dialog.QFileDialog.getSaveFileName",
                return_value=(target, selected),
            ) as chooser:
                self.dialog.job_actions["save_as"].trigger()
            filters = chooser.call_args.args[3]
            self.assertIn(DATABASE_FILTER, filters)
            self.assertIn(LEGACY_LIST_FILTER, filters)
            path = target + extension
            jobs, archived = self.store.read_job_list(path)
            self.assertEqual([job.id for job in jobs], ["synthetic"])
            self.assertFalse(archived)

    def test_empty_database_can_be_saved_and_opened(self):
        self.store.jobs.clear()
        target = str(Path(self.tmp) / "new.pmejdb")
        with patch(
            "job_manager.jobs_dialog.QFileDialog.getSaveFileName",
            return_value=(target, DATABASE_FILTER),
        ):
            self.dialog.job_actions["save_as"].trigger()
        self.assertEqual(read_document(target)["jobs"], [])
        with patch("job_manager.jobs_dialog.QMessageBox.question", return_value=QMessageBox.Yes):
            self.assertTrue(self.dialog.open_job_list(target))
        self.assertEqual(self.store.jobs_path, target)

    def test_open_filter_and_drop_route_accept_both_extensions(self):
        from .test_dialogs import TestDropOpensAJobList

        for extension in (".pmejdb", ".pmejbs"):
            self.assertIn("*" + extension, JOB_LIST_FILTER)
            path = str(Path(self.tmp) / ("drop" + extension))
            self.store.export_jobs(path)
            event = TestDropOpensAJobList.drop_event(self, path)
            with patch.object(self.dialog, "open_job_list") as opened:
                self.dialog.dropEvent(event)
            opened.assert_called_once_with(path)

    def test_saved_copy_is_independent_of_settings_and_live_store(self):
        target = str(Path(self.tmp) / "share.pmejdb")
        self.store.export_jobs(target)
        recipient = JobStore(str(Path(self.tmp) / "recipient"))
        recipient.use_jobs_file(target)
        self.assertEqual(set(recipient.jobs), {"synthetic"})
        self.assertEqual(recipient.hosts, {})
