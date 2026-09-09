"""The About window: the version it reports, and that it reports the real one."""

from __future__ import annotations

import unittest
import unittest.mock

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from job_manager import PLUGIN_NAME, PLUGIN_VERSION  # noqa: E402
from job_manager.about_dialog import REPO_URL, AboutDialog  # noqa: E402


class AboutTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.dialog = AboutDialog(parent=None)
        self.addCleanup(self.dialog.deleteLater)


class TestWhatItSays(AboutTestCase):
    def test_it_names_the_version_that_is_actually_running(self):
        # Read from the module rather than written out here: a hand-copied
        # number is one that goes stale at the next release and then tells a
        # bug report the wrong thing.
        self.assertIn(PLUGIN_VERSION, self.dialog.lbl_version.text())

    def test_the_version_line_is_what_a_report_should_quote(self):
        self.assertEqual(self.dialog.version_line(), f"{PLUGIN_NAME} {PLUGIN_VERSION}")

    def test_copy_puts_it_on_the_clipboard(self):
        self.dialog.btn_copy.click()
        self.assertEqual(QApplication.clipboard().text(), self.dialog.version_line())

    def test_it_carries_the_plugin_icon(self):
        # The same drawing as the tab and the task bar; a different one here
        # would make the plugin look like two things.
        label = self.dialog.findChildren(type(self.dialog.lbl_version))[0]
        self.assertIsNotNone(label)
        pixmaps = [
            child.pixmap()
            for child in self.dialog.findChildren(type(self.dialog.lbl_version))
            if not child.pixmap().isNull()
        ]
        self.assertTrue(pixmaps, "no icon in the About window")
        self.assertEqual(pixmaps[0].width(), 64)

    def test_the_repository_is_reachable_and_selectable(self):
        # Clickable is not enough: a machine with no browser configured opens
        # nothing, and then the text itself is the only way to get the URL.
        from PyQt6.QtCore import Qt

        links = [
            child
            for child in self.dialog.findChildren(type(self.dialog.lbl_version))
            if REPO_URL in child.text()
        ]
        self.assertTrue(links, "the repository URL is not shown")
        flags = links[0].textInteractionFlags()
        self.assertTrue(flags & Qt.TextInteractionFlag.TextSelectableByMouse)

    def test_it_does_not_dump_the_installer_blurb(self):
        # PLUGIN_DESCRIPTION is a paragraph written for the catalogue. Someone
        # who opened this to read a version number should not have to scroll
        # past it.
        from job_manager import PLUGIN_DESCRIPTION

        shown = " ".join(
            child.text() for child in self.dialog.findChildren(type(self.dialog.lbl_version))
        )
        self.assertNotIn(PLUGIN_DESCRIPTION, shown)
        self.assertLess(len(shown), len(PLUGIN_DESCRIPTION))


class TestItIsReachable(unittest.TestCase):
    def test_the_menu_entry_opens_it(self):
        from job_manager import show_about

        with unittest.mock.patch("job_manager.about_dialog.AboutDialog.exec") as shown:
            show_about(context=None)
        shown.assert_called_once()

    def test_a_failure_to_open_is_reported_not_raised(self):
        from job_manager import show_about

        context = unittest.mock.MagicMock()
        with unittest.mock.patch(
            "job_manager.about_dialog.AboutDialog.__init__", side_effect=RuntimeError("boom")
        ):
            show_about(context=context)
        context.show_status_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
