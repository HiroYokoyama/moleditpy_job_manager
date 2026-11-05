"""The job file browser, reused for the host's remote root, a folder at a time."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QMessageBox,
    QPushButton,
    QTreeWidgetItem,
    QWidget,
)

from job_manager import host_file_browser  # noqa: E402
from job_manager.host_file_browser import HostFileBrowserDialog  # noqa: E402
from job_manager.jobs_dialog import JobsDialog  # noqa: E402
from job_manager.loading_dialog import (  # noqa: E402
    SHOW_AFTER_MS,
    close_loading_dialog,
    open_loading_dialog,
)
from job_manager.models import BACKEND_JOB_SERVER  # noqa: E402
from job_manager.tasks import gone  # noqa: E402
from job_manager.output_file_dialog import OutputFileSelectorDialog  # noqa: E402
from job_manager.tree_utils import PATH_ROLE, collect_tree_leaves  # noqa: E402

from .fakes import make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class BrowserTestCase(DialogTestCase):
    """The host's folders are ``self.folders``; a name ending in / is a folder."""

    def setUp(self):
        super().setUp()
        self.folders = {"": []}
        self.asked = []
        self.held = None
        patcher = patch.object(self.service, "list_host_path", side_effect=self.list_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def list_path(self, host, path, depth, done, failed, owner=None):
        self.assertEqual(depth, 1)
        relative = path[len(self.host.remote_root) :].strip("/")
        self.asked.append(relative)
        if self.held is not None:
            self.held.append((relative, done, failed))
        elif relative in self.folders:
            done(list(self.folders[relative]))
        else:
            failed("no route")

    def browser(self, on_text=None):
        dialog = HostFileBrowserDialog(
            self.service, self.host, on_text_callback=on_text or MagicMock()
        )
        self.addCleanup(dialog.deleteLater)
        return dialog

    @staticmethod
    def rows(dialog):
        return sorted(item.data(0, PATH_ROLE) for item in collect_tree_leaves(dialog.tree))

    @staticmethod
    def top(dialog, name):
        for row in range(dialog.tree.topLevelItemCount()):
            item = dialog.tree.topLevelItem(row)
            if item.text(0) == name:
                return item
        raise AssertionError(f"{name} is not listed")

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
        self.assertIsInstance(self.browser(), OutputFileSelectorDialog)

    def test_opening_the_window_reads_only_the_root(self):
        self.folders.update({"": ["job1/", "a.txt"], "job1": ["b.out"]})
        dialog = self.browser()
        self.assertEqual(self.asked, [""])
        self.assertEqual(self.rows(dialog), ["a.txt"])
        self.assertIn(self.host.remote_root, dialog.lbl_headline.text())

    def test_a_folder_shows_an_arrow_before_it_is_read(self):
        self.folders[""] = ["job1/"]
        folder = self.top(self.browser(), "job1")
        self.assertEqual(folder.childCount(), 0)
        self.assertEqual(
            folder.childIndicatorPolicy(), QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
        )

    def test_opening_a_folder_reads_that_folder_once(self):
        self.folders.update({"": ["job1/"], "job1": ["b.out"]})
        dialog = self.browser()
        folder = self.top(dialog, "job1")
        folder.setExpanded(True)
        self.assertEqual(self.rows(dialog), ["job1/b.out"])
        folder.setExpanded(False)
        folder.setExpanded(True)
        self.assertEqual(self.asked, ["", "job1"])

    def test_folders_nest_with_the_path_from_the_root(self):
        self.folders.update({"": ["a/"], "a": ["b/"], "a/b": ["c.log"]})
        dialog = self.browser()
        outer = self.top(dialog, "a")
        outer.setExpanded(True)
        outer.child(0).setExpanded(True)
        self.assertEqual(self.rows(dialog), ["a/b/c.log"])

    def test_folders_come_before_files(self):
        self.folders[""] = ["z.txt", "b/", "A/", "a.txt"]
        dialog = self.browser()
        names = [dialog.tree.topLevelItem(i).text(0) for i in range(4)]
        self.assertEqual(names, ["A", "b", "a.txt", "z.txt"])

    def test_hidden_names_are_left_out(self):
        self.folders[""] = [".ssh/", ".bashrc", "a.txt"]
        self.assertEqual(self.rows(self.browser()), ["a.txt"])

    def test_an_empty_folder_loses_its_arrow(self):
        self.folders.update({"": ["empty/"], "empty": []})
        dialog = self.browser()
        folder = self.top(dialog, "empty")
        folder.setExpanded(True)
        self.assertEqual(
            folder.childIndicatorPolicy(),
            QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless,
        )
        self.assertIn("0 item(s) in empty", dialog.lbl_status.text())

    def test_a_folder_too_long_to_show_says_so(self):
        self.folders[""] = ["a", "b", "c"]
        with patch.object(host_file_browser, "MAX_LISTED_FILES", 2):
            dialog = self.browser()
        self.assertEqual(len(self.rows(dialog)), 2)
        self.assertIn("first 2 of 3", dialog.lbl_status.text())

    def test_a_failed_root_is_shown(self):
        del self.folders[""]
        dialog = self.browser()
        self.assertIn("no route", dialog.lbl_status.text())
        self.assertTrue(dialog.btn_browse_remote.isEnabled())

    def test_a_failed_folder_closes_and_opening_it_again_tries_again(self):
        self.folders[""] = ["job1/"]
        dialog = self.browser()
        folder = self.top(dialog, "job1")
        folder.setExpanded(True)
        self.assertIn("no route", dialog.lbl_status.text())
        self.assertFalse(folder.isExpanded())
        self.folders["job1"] = ["b.out"]
        folder.setExpanded(True)
        self.assertEqual(self.rows(dialog), ["job1/b.out"])

    def test_refresh_reads_the_root_and_the_open_folders_again(self):
        self.folders.update({"": ["job1/", "job2/"], "job1": ["b.out"]})
        dialog = self.browser()
        self.top(dialog, "job1").setExpanded(True)
        self.folders["job1"] = ["new.out"]
        self.asked.clear()
        dialog.btn_browse_remote.click()
        self.assertEqual(self.asked, ["", "job1"])
        self.assertEqual(self.rows(dialog), ["job1/new.out"])
        self.assertTrue(self.top(dialog, "job1").isExpanded())

    def test_a_reply_from_before_a_refresh_is_ignored(self):
        self.folders.update({"": ["job1/"], "job1": ["old.out"]})
        dialog = self.browser()
        self.held = []
        self.top(dialog, "job1").setExpanded(True)
        (_path, late, _failed) = self.held.pop()
        self.held = None
        self.folders["job1"] = ["new.out"]
        dialog.btn_browse_remote.click()
        late(["old.out"])
        self.assertEqual(self.rows(dialog), ["job1/new.out"])

    def test_the_filter_says_it_covers_what_has_been_read(self):
        self.assertIn("opened so far", self.browser().txt_filter.placeholderText())


class TestLoadingWindowTiming(unittest.TestCase):
    def setUp(self):
        self.parent = QWidget()
        self.parent.show()
        self.addCleanup(self.parent.deleteLater)
        self.cancelled = []

    def open(self):
        return open_loading_dialog(self.parent, "t", "x", lambda: self.cancelled.append(True))

    def test_a_slow_reply_gets_the_window_soon(self):
        dialog = self.open()
        self.assertFalse(dialog.isVisible())
        QTest.qWait(SHOW_AFTER_MS + 300)
        self.assertTrue(dialog.isVisible())
        close_loading_dialog(dialog)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.cancelled, [])

    def test_a_quick_reply_never_shows_it(self):
        dialog = self.open()
        close_loading_dialog(dialog)
        QTest.qWait(SHOW_AFTER_MS + 200)
        self.assertTrue(gone(dialog) or not dialog.isVisible())

    def test_the_cancel_button_reaches_the_caller(self):
        dialog = self.open()
        QTest.qWait(SHOW_AFTER_MS + 300)
        dialog.findChild(QPushButton).click()
        self.assertEqual(self.cancelled, [True])


class TestLoadingWindow(BrowserTestCase):
    def test_it_stays_up_while_a_folder_is_read_and_closes_with_the_reply(self):
        self.folders[""] = ["job1/"]
        dialog = self.browser()
        self.assertIsNone(dialog._loading)
        self.held = []
        self.top(dialog, "job1").setExpanded(True)
        self.assertIn("job1", dialog._loading.labelText())
        self.held.pop()[1](["b.out"])
        self.assertIsNone(dialog._loading)
        self.assertEqual(self.rows(dialog), ["job1/b.out"])

    def test_it_closes_when_a_folder_fails(self):
        self.folders[""] = ["job1/"]
        dialog = self.browser()
        self.held = []
        self.top(dialog, "job1").setExpanded(True)
        self.held.pop()[2]("no route")
        self.assertIsNone(dialog._loading)

    def test_closing_it_with_the_reply_is_not_a_cancel(self):
        self.folders[""] = ["job1/"]
        dialog = self.browser()
        self.held = []
        self.top(dialog, "job1").setExpanded(True)
        self.held.pop()[1](["b.out"])
        self.assertNotIn("Cancelled", dialog.lbl_status.text())

    def test_cancel_closes_the_folder_and_ignores_the_late_reply(self):
        self.folders[""] = ["job1/"]
        dialog = self.browser()
        self.held = []
        folder = self.top(dialog, "job1")
        folder.setExpanded(True)
        late = self.held.pop()[1]
        dialog._loading.cancel()
        dialog._loading.canceled.emit()
        self.assertIsNone(dialog._loading)
        self.assertFalse(folder.isExpanded())
        late(["b.out"])
        self.assertEqual(self.rows(dialog), [])
        self.held = None
        self.folders["job1"] = ["b.out"]
        folder.setExpanded(True)
        self.assertEqual(self.rows(dialog), ["job1/b.out"])

    def test_cancelling_the_root_leaves_refresh_usable(self):
        self.held = []
        dialog = self.browser()
        self.assertFalse(dialog.btn_browse_remote.isEnabled())
        dialog._loading.canceled.emit()
        self.assertTrue(dialog.btn_browse_remote.isEnabled())
        self.assertEqual(dialog.lbl_status.text(), "Cancelled.")

    def test_it_stays_up_while_a_file_is_fetched(self):
        self.folders[""] = ["a.txt"]
        on_text = MagicMock()
        dialog = self.browser(on_text=on_text)
        checks = []
        with patch.object(
            self.service,
            "stat_host_path",
            side_effect=lambda h, p, d, ok, err, owner=None: checks.append(ok),
        ):
            dialog._open_item(self.first(dialog), as_text=True)
        self.assertIn("a.txt", dialog._loading.labelText())
        with patch(
            "job_manager.runner.stat_host_path",
            return_value={"type": "file", "exists": True, "size": 10},
        ):
            checks[0]({"size": 10})
        self.assertIsNone(dialog._loading)
        on_text.assert_called_once()

    def test_cancelling_a_file_opens_nothing_when_the_reply_comes(self):
        self.folders[""] = ["a.txt"]
        on_text = MagicMock()
        dialog = self.browser(on_text=on_text)
        checks = []
        with patch.object(
            self.service,
            "stat_host_path",
            side_effect=lambda h, p, d, ok, err, owner=None: checks.append(ok),
        ):
            dialog._open_item(self.first(dialog), as_text=True)
        dialog._loading.canceled.emit()
        checks[0]({"size": 10})
        self.assertEqual(self.transport.downloads, [])
        on_text.assert_not_called()
        self.assertTrue(dialog.btn_open.isEnabled())
        self.assertEqual(dialog._pending, [])

    def test_it_makes_way_for_the_large_file_question(self):
        self.folders[""] = ["big.out"]
        dialog = self.browser()
        seen = []

        def question(*_args):
            seen.append(dialog._loading)
            return QMessageBox.StandardButton.No

        with patch.object(QMessageBox, "question", side_effect=question):
            self.fetched(dialog, self.first(dialog), size=host_file_browser.LARGE_FILE_BYTES + 1)
        self.assertEqual(seen, [None])

    def test_closing_the_browser_closes_it(self):
        self.held = []
        dialog = self.browser()
        dialog.reject()
        self.assertIsNone(dialog._loading)
        late = self.held.pop()[1]
        late(["a.txt"])
        self.assertEqual(self.rows(dialog), [])


class TestServiceListing(DialogTestCase):
    def test_one_folder_is_asked_for_and_not_the_tree_below_it(self):
        self.transport.when("ls -p", stdout="sub/\nz.txt\n")
        got = []
        with patch("job_manager.runner.require_remote_path"):
            self.service.list_host_path(self.host, "/x", 1, got.append, self.fail)
        self.assertEqual(got, [["sub/", "z.txt"]])
        self.assertFalse([cmd for cmd in self.transport.commands if "find " in cmd])


class TestOpening(BrowserTestCase):
    def test_a_file_is_fetched_without_asking_and_shown_as_text(self):
        self.folders.update({"": ["job1/"], "job1": ["b.out"]})
        on_text = MagicMock()
        dialog = self.browser(on_text=on_text)
        self.top(dialog, "job1").setExpanded(True)
        with patch.object(QMessageBox, "question") as asked:
            self.fetched(dialog, self.first(dialog))
        asked.assert_not_called()
        self.assertEqual(self.transport.downloads[0][0], f"{self.host.remote_root}/job1/b.out")
        shown = on_text.call_args.args[0].replace("\\", "/")
        self.assertTrue(shown.endswith("/job1/b.out"), shown)
        self.assertTrue(os.path.isfile(on_text.call_args.args[0]))

    def test_two_files_called_the_same_do_not_share_a_copy(self):
        self.folders.update({"": ["job1/", "job2/"], "job1": ["x.out"], "job2": ["x.out"]})
        on_text = MagicMock()
        dialog = self.browser(on_text=on_text)
        self.top(dialog, "job1").setExpanded(True)
        self.top(dialog, "job2").setExpanded(True)
        for item in collect_tree_leaves(dialog.tree):
            self.fetched(dialog, item)
        first, second = (call.args[0] for call in on_text.call_args_list)
        self.assertNotEqual(os.path.normpath(first), os.path.normpath(second))

    def test_opening_again_reads_the_host_again(self):
        self.folders[""] = ["b.out"]
        dialog = self.browser()
        self.fetched(dialog, self.first(dialog))
        self.fetched(dialog, self.first(dialog))
        self.assertEqual(len(self.transport.downloads), 2)

    def test_a_very_large_file_asks_first(self):
        self.folders[""] = ["big.out"]
        dialog = self.browser()
        huge = host_file_browser.LARGE_FILE_BYTES + 1
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ) as asked:
            self.fetched(dialog, self.first(dialog), size=huge)
        asked.assert_called_once()
        self.assertEqual(self.transport.downloads, [])
        self.assertTrue(dialog.btn_open.isEnabled())

    def test_a_failed_fetch_is_shown_and_another_can_be_tried(self):
        self.folders[""] = ["b.out"]
        dialog = self.browser()
        with patch.object(
            self.service,
            "stat_host_path",
            side_effect=lambda h, p, d, ok, err, owner=None: err("gone"),
        ):
            dialog._open_item(self.first(dialog), as_text=True)
        self.assertIn("gone", dialog.lbl_status.text())
        self.assertEqual(dialog._pending, [])

    def test_a_link_to_a_folder_says_so_and_fetches_nothing(self):
        self.folders[""] = ["link"]
        dialog = self.browser()
        with patch.object(
            self.service,
            "stat_host_path",
            side_effect=lambda h, p, d, ok, err, owner=None: ok({"type": "directory"}),
        ):
            dialog._open_item(self.first(dialog), as_text=True)
        self.assertIn("link to a folder", dialog.lbl_status.text())
        self.assertEqual(self.transport.downloads, [])

    def test_a_folder_opens_nothing(self):
        self.folders[""] = ["job1/"]
        dialog = self.browser()
        with patch.object(self.service, "stat_host_path") as stat:
            dialog._open_item(self.top(dialog, "job1"), as_text=True)
        stat.assert_not_called()

    def test_a_mirrored_file_opens_from_the_mirror_without_a_download(self):
        mirror = os.path.join(self.tmp, "mirror_copy")
        with open(mirror, "w", encoding="utf-8") as handle:
            handle.write("text\n")
        self.folders[""] = ["mirror_copy"]
        with patch.object(
            HostFileBrowserDialog, "_mirrored_job_dir", return_value=os.path.dirname(mirror)
        ):
            dialog = self.browser()
        self.assertEqual(self.first(dialog).text(3), "Mirror (no download needed)")


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
