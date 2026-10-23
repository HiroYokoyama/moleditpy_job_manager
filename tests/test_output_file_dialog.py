"""Tests for OutputFileSelectorDialog and temporary output file caching."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from job_manager.models import Job, new_id
from job_manager.output_file_dialog import (
    OutputFileSelectorDialog,
    describe_file_type,
    format_file_size,
    IS_REMOTE_ROLE,
    PATH_ROLE,
)

app = QApplication.instance() or QApplication([])


class TestOutputFileHelpers(unittest.TestCase):
    def test_describe_file_type(self):
        self.assertIn("ORCA", describe_file_type("opt.out"))
        self.assertIn("Gaussian", describe_file_type("calc.log"))
        self.assertIn("XYZ", describe_file_type("geom.xyz"))
        self.assertIn("Checkpoint", describe_file_type("calc.fchk"))
        self.assertIn("JSON", describe_file_type("results.json"))
        self.assertIn("CSV", describe_file_type("summary.csv"))
        self.assertEqual(describe_file_type("unknown.dat123"), "DAT123 File")

    def test_format_file_size(self):
        self.assertEqual(format_file_size(500), "500 B")
        self.assertEqual(format_file_size(2048), "2.0 KB")
        self.assertEqual(format_file_size(3 * 1024 * 1024), "3.0 MB")


class TestOutputFileSelectorDialog(unittest.TestCase):
    def test_cached_subdirectory_does_not_replace_a_same_named_root_file(self):
        cache = os.path.join(self.temp_dir, "cache")
        path = os.path.join(cache, "nested", "result.out")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("nested result\n")
        self.job.local_dir = ""
        from unittest.mock import patch

        with patch.object(OutputFileSelectorDialog, "_cache_dir", return_value=cache):
            dialog = OutputFileSelectorDialog(self.service, self.job)
            self.addCleanup(dialog.deleteLater)
            dialog._populate_tree_remote(["result.out", "nested/result.out"])
        rows = {item.data(0, PATH_ROLE): item for item in dialog._all_items}
        self.assertTrue(rows["result.out"].data(0, IS_REMOTE_ROLE))
        self.assertFalse(rows[path].data(0, IS_REMOTE_ROLE))
        self.assertEqual(dialog.tree.currentItem().data(0, PATH_ROLE), "result.out")

    def test_remote_refresh_preserves_active_filter(self):
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)
        dialog.txt_filter.setText("xyz")
        dialog._populate_tree_remote(["result.out", "geometry.xyz"])
        rows = {item.text(0): item for item in dialog._all_items}
        self.assertTrue(rows["result.out"].isHidden())
        self.assertFalse(rows["geometry.xyz"].isHidden())

    def test_browser_button_labels_fit_at_default_size(self):
        path = os.path.join(self.temp_dir, "result.out")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("complete\n")
        self.job.downloaded_files = [path]
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.addCleanup(dialog.hide)
        QApplication.instance().processEvents()
        for button in (
            dialog.btn_open,
            dialog.btn_text,
            dialog.btn_close,
            dialog.btn_open_folder,
            dialog.btn_browse_remote,
        ):
            with self.subTest(label=button.text()):
                self.assertGreaterEqual(button.width(), button.sizeHint().width())
                self.assertTrue(
                    dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
                )
        buttons = (
            dialog.btn_open,
            dialog.btn_text,
            dialog.btn_close,
            dialog.btn_open_folder,
            dialog.btn_browse_remote,
        )
        self.assertEqual(
            len({button.mapTo(dialog, button.rect().center()).y() for button in buttons}), 1
        )
        self.assertGreaterEqual(dialog.width(), 960)

    def test_double_click_views_text_but_open_button_uses_host(self):
        path = os.path.join(self.temp_dir, "result.out")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Calculation complete\n")
        self.job.downloaded_files = [path]
        viewed, opened = [], []
        dialog = OutputFileSelectorDialog(
            self.service, self.job, on_open_callback=opened.append, on_text_callback=viewed.append
        )
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.addCleanup(dialog.hide)
        item = dialog.tree.currentItem()
        dialog.tree.itemDoubleClicked.emit(item, 0)
        self.assertEqual(viewed, [path])
        self.assertEqual(opened, [])
        self.assertTrue(dialog.isVisible())
        dialog.btn_text.click()
        self.assertEqual(viewed, [path, path])
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.btn_open.text(), "Open in MoleditPy")
        dialog.btn_open.click()
        self.assertEqual(opened, [path])
        self.assertFalse(dialog.isVisible())

    def test_binary_double_click_keeps_browser_open(self):
        path = os.path.join(self.temp_dir, "binary.txt")
        with open(path, "wb") as handle:
            handle.write(b"header\x00\x01\x02")
        self.job.downloaded_files = [path]
        viewed, opened = [], []
        dialog = OutputFileSelectorDialog(
            self.service, self.job, on_open_callback=opened.append, on_text_callback=viewed.append
        )
        self.addCleanup(dialog.deleteLater)
        dialog.tree.itemDoubleClicked.emit(dialog.tree.currentItem(), 0)
        self.assertEqual(viewed, [])
        self.assertEqual(opened, [])
        self.assertIn("binary", dialog.lbl_status.text())
        self.assertEqual(dialog.result(), 0)

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.service = MagicMock()
        self.job = Job(
            id=new_id(),
            name="benzene_opt",
            host_id="h1",
            remote_dir="/scratch/user/benzene_opt",
            local_dir=self.temp_dir,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_local_files_populates_tree(self):
        file1 = os.path.join(self.temp_dir, "benzene.out")
        file2 = os.path.join(self.temp_dir, "benzene.xyz")
        with open(file1, "w") as f:
            f.write("ORCA calculation output")
        with open(file2, "w") as f:
            f.write("6\n\nC 0 0 0\n")

        self.job.downloaded_files = [file1, file2]

        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        item0 = dialog.tree.topLevelItem(0)
        item1 = dialog.tree.topLevelItem(1)
        names = {item0.text(0), item1.text(0)}
        self.assertEqual(names, {"benzene.out", "benzene.xyz"})
        # benzene.out is prioritized
        self.assertEqual(dialog.tree.currentItem().text(0), "benzene.out")

    def test_local_wrapper_log_is_not_preselected_over_a_molecular_result(self):
        paths = [os.path.join(self.temp_dir, name) for name in ("job.log", "result.xyz")]
        for path in paths:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("output\n")
        self.job.log_file = "job.log"
        self.job.downloaded_files = paths
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.tree.currentItem().text(0), "result.xyz")

    def test_filter_hides_non_matching_files(self):
        file1 = os.path.join(self.temp_dir, "benzene.out")
        file2 = os.path.join(self.temp_dir, "benzene.xyz")
        with open(file1, "w") as f:
            f.write("output")
        with open(file2, "w") as f:
            f.write("xyz")

        self.job.downloaded_files = [file1, file2]
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        dialog.txt_filter.setText("xyz")
        visible = [
            dialog.tree.topLevelItem(i).text(0)
            for i in range(dialog.tree.topLevelItemCount())
            if not dialog.tree.topLevelItem(i).isHidden()
        ]
        self.assertEqual(visible, ["benzene.xyz"])

    def test_open_local_file_calls_callback(self):
        file1 = os.path.join(self.temp_dir, "benzene.out")
        with open(file1, "w") as f:
            f.write("output")

        self.job.downloaded_files = [file1]
        opened = []
        dialog = OutputFileSelectorDialog(
            self.service, self.job, on_open_callback=lambda p: opened.append(p)
        )
        self.addCleanup(dialog.deleteLater)

        dialog.btn_open.click()
        self.assertEqual(opened, [file1])

    def test_remote_files_are_listed_and_still_openable(self):
        # No local files exist
        self.job.downloaded_files = []
        self.job.local_dir = ""

        # Setup mock for service.list_remote_results
        def fake_list(job, on_ok, on_error, owner=None):
            on_ok(["remote_calc.out", "remote_geom.xyz"])

        self.service.list_remote_results.side_effect = fake_list

        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        # Dialog should list the 2 remote files
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        current = dialog.tree.currentItem()
        self.assertIsNotNone(current)
        self.assertEqual(current.text(0), "remote_calc.out")
        self.assertTrue(current.data(0, IS_REMOTE_ROLE))

        # Open is enabled for a remote file too: clicking it now offers to
        # download and open rather than silently doing nothing, which is what
        # a disabled button with no explanation looked like before.
        self.assertTrue(dialog.btn_open.isEnabled())

    def test_remote_files_are_greyed_out(self):
        self.job.downloaded_files = []
        self.job.local_dir = ""
        self.service.list_remote_results.side_effect = lambda job, on_ok, on_error, owner=None: (
            on_ok(["remote_calc.out"])
        )
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        item = dialog.tree.topLevelItem(0)
        from PySide6.QtGui import QColor

        self.assertEqual(item.foreground(0).color(), QColor("#8b949e"))
        self.assertTrue(item.font(0).italic())

    def test_folders_are_built_from_slash_separated_names(self):
        self.job.downloaded_files = []
        self.job.local_dir = ""
        self.service.list_remote_results.side_effect = lambda job, on_ok, on_error, owner=None: (
            on_ok(["top.out", "scratch/benzene.tmp", "scratch/nested/deep.log"])
        )
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        # One top-level file and one top-level folder -- not three flat rows.
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        names = {dialog.tree.topLevelItem(i).text(0) for i in range(2)}
        self.assertEqual(names, {"top.out", "scratch"})

        folder = next(
            dialog.tree.topLevelItem(i)
            for i in range(2)
            if dialog.tree.topLevelItem(i).text(0) == "scratch"
        )
        self.assertEqual(folder.childCount(), 2)
        child_names = {folder.child(i).text(0) for i in range(2)}
        self.assertEqual(child_names, {"benzene.tmp", "nested"})

    def test_a_folder_row_is_not_selectable(self):
        self.job.downloaded_files = []
        self.job.local_dir = ""
        self.service.list_remote_results.side_effect = lambda job, on_ok, on_error, owner=None: (
            on_ok(["scratch/benzene.tmp"])
        )
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        folder = dialog.tree.topLevelItem(0)
        self.assertEqual(folder.text(0), "scratch")
        self.assertIsNone(folder.data(0, IS_REMOTE_ROLE))
        # Double-clicking a folder must not crash trying to open it.
        dialog._open_item(folder)

    def test_a_mirrored_file_opens_without_downloading(self):
        from job_manager.models import HostProfile

        mirror_root = tempfile.mkdtemp(prefix="mirror_")
        self.addCleanup(lambda: __import__("shutil").rmtree(mirror_root, ignore_errors=True))
        job_dir = os.path.join(mirror_root, "benzene_opt")
        os.makedirs(job_dir)
        with open(os.path.join(job_dir, "remote_calc.out"), "w") as f:
            f.write("mirrored")

        host = HostProfile(id="h1", remote_root="/scratch/user", equal_path=mirror_root)
        self.service.store.hosts = {"h1": host}
        self.job.downloaded_files = []
        self.job.local_dir = ""
        self.service.list_remote_results.side_effect = lambda job, on_ok, on_error, owner=None: (
            on_ok(["remote_calc.out"])
        )

        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        item = dialog.tree.topLevelItem(0)
        self.assertFalse(item.data(0, IS_REMOTE_ROLE))
        self.assertEqual(item.data(0, PATH_ROLE), os.path.join(job_dir, "remote_calc.out"))
        self.service.download.assert_not_called()

    def test_no_mirror_configured_still_shows_it_as_remote(self):
        from job_manager.models import HostProfile

        host = HostProfile(id="h1", remote_root="/scratch/user", equal_path="")
        self.service.store.hosts = {"h1": host}
        self.job.downloaded_files = []
        self.job.local_dir = ""
        self.service.list_remote_results.side_effect = lambda job, on_ok, on_error, owner=None: (
            on_ok(["remote_calc.out"])
        )

        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        item = dialog.tree.topLevelItem(0)
        self.assertTrue(item.data(0, IS_REMOTE_ROLE))

    def test_local_files_build_tree_hierarchy(self):
        file1 = os.path.join(self.temp_dir, "benzene.out")
        sub_dir = os.path.join(self.temp_dir, "scratch")
        os.makedirs(sub_dir, exist_ok=True)
        file2 = os.path.join(sub_dir, "temp.xyz")

        with open(file1, "w") as f:
            f.write("output")
        with open(file2, "w") as f:
            f.write("xyz")

        self.job.downloaded_files = [file1, file2]
        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        # 1 top-level file ("benzene.out") and 1 top-level folder ("scratch")
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        names = {dialog.tree.topLevelItem(i).text(0) for i in range(2)}
        self.assertEqual(names, {"benzene.out", "scratch"})

        folder = next(
            dialog.tree.topLevelItem(i)
            for i in range(2)
            if dialog.tree.topLevelItem(i).text(0) == "scratch"
        )
        self.assertEqual(folder.childCount(), 1)
        self.assertEqual(folder.child(0).text(0), "temp.xyz")

    def test_job_folder_only_does_not_leak_unrelated_workspace_files(self):
        # A shared working directory with multiple unrelated files
        shared_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(shared_dir, ignore_errors=True))

        my_output = os.path.join(shared_dir, "my_job.out")
        other_file = os.path.join(shared_dir, "other_job.out")
        with open(my_output, "w") as f:
            f.write("my calc")
        with open(other_file, "w") as f:
            f.write("other calc")

        self.job.local_dir = shared_dir
        self.job.downloaded_files = [my_output]

        dialog = OutputFileSelectorDialog(self.service, self.job)
        self.addCleanup(dialog.deleteLater)

        existing = dialog._get_existing_local_files()
        self.assertIn(os.path.normpath(my_output), [os.path.normpath(p) for p in existing])
        self.assertNotIn(os.path.normpath(other_file), [os.path.normpath(p) for p in existing])


class _FakeService(QObject):
    """Just enough service to hold real signals the dialog can connect to.

    A MagicMock cannot answer "is anything still connected?", which is the
    whole question here: the service outlives this window, so a handler left
    attached is a closure holding a deleted widget.
    """

    results_ready = Signal(str, list)
    download_failed = Signal(str, str)
    error = Signal(str)
    message = Signal(str)

    def __init__(self, started: bool = True) -> None:
        super().__init__()
        self.started = started
        self.calls: list = []
        # The dialog asks for the host to work out an equal-path mirror.
        self.store = MagicMock()
        self.store.hosts.get.return_value = None
        # With no local files the dialog offers to list the host; nothing here
        # is testing that, so it is answered with an empty listing.
        self.list_remote_results = MagicMock()

    def download(self, job, into: str = "", names=None) -> bool:
        self.calls.append(names)
        return self.started


class TestAPendingDownloadIsNotLeftConnected(unittest.TestCase):
    """The service outlives the window; a handler that survives it fires into
    a deleted widget the next time this job downloads."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.job = Job(
            id=new_id(),
            name="benzene_opt",
            host_id="h1",
            remote_dir="/scratch/user/benzene_opt",
            local_dir=self.temp_dir,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def dialog(self, service):
        dlg = OutputFileSelectorDialog(service, self.job)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_downloaded_text_keeps_browser_open_but_host_open_closes_it(self):
        path = os.path.join(self.temp_dir, "result.out")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Calculation complete\n")
        for as_text in (True, False):
            with self.subTest(as_text=as_text):
                service = _FakeService()
                dlg = self.dialog(service)
                viewed, opened = [], []
                dlg.on_text_callback = viewed.append
                dlg.on_open_callback = opened.append
                dlg.show()
                self.addCleanup(dlg.hide)
                dlg._download_and_open(None, "result.out", as_text=as_text)
                service.results_ready.emit(self.job.id, [path])
                self.assertEqual(viewed, [path] if as_text else [])
                self.assertEqual(opened, [] if as_text else [path])
                self.assertEqual(dlg.isVisible(), as_text)
                self.assertFalse(dlg._pending)

    def test_closing_mid_download_disconnects(self):
        service = _FakeService()
        dlg = self.dialog(service)
        dlg._download_and_open(None, "benzene.out")
        self.assertTrue(dlg._pending)

        dlg.reject()

        self.assertFalse(dlg._pending)
        # Nothing left listening: emitting must not reach the closed window.
        service.results_ready.emit(self.job.id, [])
        service.error.emit(f"{self.job.name} exploded")
        service.download_failed.emit(self.job.id, "connection reset")

    def test_transfer_failure_unblocks_browser_and_ignores_another_job(self):
        service = _FakeService()
        dlg = self.dialog(service)
        dlg._download_and_open(None, "result.out", as_text=True)
        service.download_failed.emit("another-job", "connection reset")
        self.assertTrue(dlg._pending)
        service.download_failed.emit(self.job.id, "connection reset")
        self.assertFalse(dlg._pending)
        self.assertTrue(dlg.btn_open.isEnabled())
        self.assertIn("connection reset", dlg.lbl_status.text())

    def test_downloaded_text_can_be_reopened_without_another_transfer(self):
        path = os.path.join(self.temp_dir, "result.out")
        service = _FakeService()
        dlg = self.dialog(service)
        dlg._populate_tree_remote(["result.out"])
        item = dlg.tree.currentItem()
        viewed = []
        dlg.on_text_callback = viewed.append
        dlg._download_and_open(item, "result.out", as_text=True)
        # Rebuilding the tree deletes the original clicked item while the transfer runs.
        dlg._populate_tree_remote(["result.out"])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("complete\n")
        service.results_ready.emit(self.job.id, [path])
        dlg.tree.itemDoubleClicked.emit(dlg.tree.currentItem(), 0)
        self.assertEqual(viewed, [path, path])
        self.assertEqual(service.calls, [["result.out"]])

    def test_a_refused_download_does_not_strand_the_window(self):
        # download() returns False when one is already running for this job,
        # and neither signal ever arrives -- so the window sat on
        # "Downloading..." with Open disabled for the rest of the session.
        service = _FakeService(started=False)
        dlg = self.dialog(service)

        dlg._download_and_open(None, "benzene.out")

        self.assertFalse(dlg._pending)
        self.assertTrue(dlg.btn_open.isEnabled())
        self.assertIn("already downloading", dlg.lbl_status.text())

    def test_a_second_request_does_not_stack_another_pair(self):
        # A double-click reaches _open_item while btn_open is disabled.
        service = _FakeService()
        dlg = self.dialog(service)

        dlg._download_and_open(None, "benzene.out")
        dlg._download_and_open(None, "benzene.out")

        self.assertEqual(len(dlg._pending), 2)
        self.assertEqual(len(service.calls), 1)
