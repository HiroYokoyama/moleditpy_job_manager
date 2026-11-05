"""The job file browser, reused for everything under a host's remote root."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from job_manager import host_file_browser  # noqa: E402
from job_manager.dialect import for_host  # noqa: E402
from job_manager.host_file_browser import HostFileBrowserDialog  # noqa: E402
from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.models import BACKEND_JOB_SERVER  # noqa: E402
from job_manager.output_file_dialog import OutputFileSelectorDialog  # noqa: E402
from job_manager.tree_utils import PATH_ROLE, collect_tree_leaves  # noqa: E402

from .fakes import make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class BrowserTestCase(DialogTestCase):
    def browser(self, *names, on_text=None):
        with patch.object(
            self.service,
            "list_host_files",
            side_effect=lambda host, done, failed, owner=None: done(list(names)),
        ):
            dialog = HostFileBrowserDialog(
                self.service, self.host, on_text_callback=on_text or MagicMock()
            )
        self.addCleanup(dialog.deleteLater)
        return dialog

    @staticmethod
    def rows(dialog):
        return sorted(item.data(0, PATH_ROLE) for item in collect_tree_leaves(dialog.tree))

    @staticmethod
    def first(dialog):
        return collect_tree_leaves(dialog.tree)[0]

    def fetched(self, dialog, item, size=10):
        """Open ``item``, with the host saying the file is ``size`` bytes."""
        with (
            patch(
                "job_manager.runner.stat_host_path",
                return_value={"type": "file", "exists": True, "size": size},
            ),
            patch.object(
                self.service,
                "stat_host_path",
                side_effect=lambda h, p, d, ok, err, owner=None: ok({"size": size}),
            ),
        ):
            dialog._open_item(item, as_text=True)


class TestListing(BrowserTestCase):
    def test_it_is_the_job_file_browser(self):
        self.assertIsInstance(self.browser("a.txt"), OutputFileSelectorDialog)

    def test_every_file_under_the_root_is_listed_at_any_depth(self):
        names = ["a.txt", "job1/b.out", "job1/deep/er/still/more/c.log"]
        dialog = self.browser(*names)
        self.assertEqual(self.rows(dialog), names)
        self.assertIn(self.host.remote_root, dialog.lbl_headline.text())

    def test_the_folders_start_closed(self):
        dialog = self.browser("job1/b.out", "job2/c.out")
        self.assertFalse(dialog.tree.topLevelItem(0).isExpanded())

    def test_a_listing_too_long_to_show_says_so(self):
        with patch.object(host_file_browser, "MAX_LISTED_FILES", 2):
            dialog = self.browser("a", "b", "c")
        self.assertEqual(len(self.rows(dialog)), 2)
        self.assertIn("first 2 of 3", dialog.lbl_status.text())

    def test_a_failed_listing_is_shown(self):
        with patch.object(
            self.service,
            "list_host_files",
            side_effect=lambda host, done, failed, owner=None: failed("no route"),
        ):
            dialog = HostFileBrowserDialog(self.service, self.host)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("no route", dialog.lbl_status.text())

    def test_refresh_lists_the_root_again(self):
        dialog = self.browser("a.txt")
        with patch.object(
            self.service,
            "list_host_files",
            side_effect=lambda host, done, failed, owner=None: done(["b.txt"]),
        ):
            dialog.btn_browse_remote.click()
        self.assertEqual(self.rows(dialog), ["b.txt"])

    def test_there_is_no_local_folder_button(self):
        dialog = self.browser("a.txt")
        self.assertNotIn(dialog.btn_open_folder, dialog.btn_open.parentWidget().buttons())


class TestServiceListing(DialogTestCase):
    def test_the_whole_tree_is_asked_for_with_no_depth_limit(self):
        self.transport.when("find .", stdout="x/y/z.txt\n")
        got = []
        with patch("job_manager.runner.require_remote_path"):
            self.service.list_host_files(self.host, got.append, self.fail)
        self.assertEqual(got, [["x/y/z.txt"]])
        command = next(cmd for cmd in self.transport.commands if "find ." in cmd)
        self.assertNotIn("maxdepth", command)

    def test_a_limited_listing_still_has_its_limit(self):
        self.assertIn("-maxdepth 3", for_host(self.host).list_tree("/x", 3))


class TestOpening(BrowserTestCase):
    def test_a_file_is_fetched_without_asking_and_shown_as_text(self):
        on_text = MagicMock()
        dialog = self.browser("job1/b.out", on_text=on_text)
        with patch.object(QMessageBox, "question") as asked:
            self.fetched(dialog, self.first(dialog))
        asked.assert_not_called()
        self.assertEqual(self.transport.downloads[0][0], f"{self.host.remote_root}/job1/b.out")
        shown = on_text.call_args.args[0].replace("\\", "/")
        self.assertTrue(shown.endswith("/job1/b.out"), shown)
        self.assertTrue(os.path.isfile(on_text.call_args.args[0]))

    def test_two_files_called_the_same_do_not_share_a_copy(self):
        on_text = MagicMock()
        dialog = self.browser("job1/x.out", "job2/x.out", on_text=on_text)
        for item in collect_tree_leaves(dialog.tree):
            self.fetched(dialog, item)
        first, second = (call.args[0] for call in on_text.call_args_list)
        self.assertNotEqual(os.path.normpath(first), os.path.normpath(second))

    def test_opening_again_reads_the_host_again(self):
        dialog = self.browser("job1/b.out")
        self.fetched(dialog, self.first(dialog))
        self.fetched(dialog, self.first(dialog))
        self.assertEqual(len(self.transport.downloads), 2)

    def test_a_very_large_file_asks_first(self):
        dialog = self.browser("big.out")
        huge = host_file_browser.LARGE_FILE_BYTES + 1
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ) as asked:
            self.fetched(dialog, self.first(dialog), size=huge)
        asked.assert_called_once()
        self.assertEqual(self.transport.downloads, [])
        self.assertTrue(dialog.btn_open.isEnabled())

    def test_a_failed_fetch_is_shown_and_another_can_be_tried(self):
        dialog = self.browser("job1/b.out")
        with patch.object(
            self.service,
            "stat_host_path",
            side_effect=lambda h, p, d, ok, err, owner=None: err("gone"),
        ):
            dialog._open_item(self.first(dialog), as_text=True)
        self.assertIn("gone", dialog.lbl_status.text())
        self.assertEqual(dialog._pending, [])

    def test_a_folder_opens_nothing(self):
        dialog = self.browser("job1/b.out")
        with patch.object(self.service, "stat_host_path") as stat:
            dialog._open_item(dialog.tree.topLevelItem(0), as_text=True)
        stat.assert_not_called()


class TestEntryPoints(DialogTestCase):
    def setUp(self):
        super().setUp()
        self.monitor = JobsDialog(self.service)
        self.addCleanup(self.monitor.deleteLater)

    def test_it_sits_beside_the_ssh_terminal_and_in_tools(self):
        action = self.monitor.job_actions["file_browser"]
        toolbar = self.monitor.toolbar.actions()
        self.assertEqual(
            toolbar.index(action), toolbar.index(self.monitor.job_actions["ssh_terminal"]) + 1
        )
        tools = next(a.menu() for a in self.monitor.menu_bar.actions() if "Tools" in a.text())
        self.assertIn(action, tools.actions())

    def test_it_opens_for_the_only_host_with_the_same_viewers_as_a_job(self):
        with patch("job_manager.host_file_browser.HostFileBrowserDialog") as browser:
            self.monitor.open_file_browser()
        args, kwargs = browser.call_args
        self.assertEqual(args, (self.service, self.host))
        self.assertIn("on_text_callback", kwargs)
        self.assertIn("on_open_callback", kwargs)
        browser.return_value.show.assert_called_once()

    def test_a_job_server_is_not_offered(self):
        self.store.add_host(make_host(id="srv", name="srv", backend=BACKEND_JOB_SERVER))
        hosts = host_file_browser.browsable_hosts(self.service)
        self.assertEqual([host.id for host in hosts], [self.host.id])
