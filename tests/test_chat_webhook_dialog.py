"""The little window the webhook URL is typed into.

What matters here is that a wrong URL is found *now*, at the keyboard, rather
than hours later when a job ends and nothing arrives.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6.QtWidgets import QApplication  # noqa: E402
from job_manager import webhook  # noqa: E402
from job_manager.chat_webhook_dialog import ChatWebhookDialog  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

SLACK_URL = "https://hooks.slack.com/services/T000/B000/xxxx"


class ChatDialogTestCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.mkdtemp(prefix="chat_hook_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = JobStore(self.tmp)

    def dialog(self):
        dialog = ChatWebhookDialog(self.store, None)
        self.addCleanup(dialog.deleteLater)
        return dialog


class TestWhatItSays(ChatDialogTestCase):
    def test_it_opens_on_the_saved_url(self):
        self.store.set_pref("notify_webhook", SLACK_URL)
        self.assertEqual(self.dialog().edit_url.text(), SLACK_URL)

    def test_an_empty_field_says_nothing_is_sent(self):
        dialog = self.dialog()
        self.assertIn("Nothing", dialog.lbl_status.text())
        self.assertFalse(dialog.btn_test.isEnabled())

    def test_the_service_is_named_back(self):
        dialog = self.dialog()
        dialog.edit_url.setText(SLACK_URL)
        self.assertIn("Slack", dialog.lbl_status.text())
        self.assertTrue(dialog.btn_test.isEnabled())

    def test_something_that_is_not_a_url_is_said_so_inline(self):
        dialog = self.dialog()
        dialog.edit_url.setText("hooks.slack.com/services/T/B/x")
        self.assertIn("http", dialog.lbl_status.text())
        self.assertFalse(dialog.btn_test.isEnabled())


class TestTheTestMessage(ChatDialogTestCase):
    def test_it_posts_the_url_in_the_field_and_not_the_saved_one(self):
        self.store.set_pref("notify_webhook", "https://example.com/old")
        dialog = self.dialog()
        dialog.edit_url.setText(SLACK_URL)
        with patch.object(webhook, "post", return_value=True) as posted:
            dialog._send_test()
            dialog.pool.waitForDone(5000)
        QApplication.processEvents()
        self.assertEqual(posted.call_args[0][0], SLACK_URL)

    def test_the_answer_comes_back_to_the_dialog(self):
        dialog = self.dialog()
        dialog.edit_url.setText(SLACK_URL)
        with patch.object(webhook, "post", return_value=False):
            dialog._send_test()
            dialog.pool.waitForDone(5000)
        QApplication.processEvents()
        self.assertIn("did not accept", dialog.lbl_status.text())

    def test_a_refused_message_says_so_and_re_enables_the_button(self):
        dialog = self.dialog()
        dialog.edit_url.setText(SLACK_URL)
        dialog._on_test_done(False)
        self.assertIn("did not accept", dialog.lbl_status.text())
        self.assertTrue(dialog.btn_test.isEnabled())

    def test_a_sent_message_says_so(self):
        dialog = self.dialog()
        dialog._on_test_done(True)
        self.assertIn("Sent", dialog.lbl_status.text())

    def test_nothing_is_posted_for_a_url_that_is_not_one(self):
        dialog = self.dialog()
        dialog.edit_url.setText("nonsense")
        with patch.object(webhook, "post") as posted:
            dialog._send_test()
        posted.assert_not_called()


class TestSaving(ChatDialogTestCase):
    def test_ok_saves_the_url(self):
        dialog = self.dialog()
        dialog.edit_url.setText("  " + SLACK_URL + "  ")
        dialog.accept()
        self.assertEqual(self.store.get_pref("notify_webhook"), SLACK_URL)

    def test_cancel_leaves_the_saved_url_alone(self):
        self.store.set_pref("notify_webhook", SLACK_URL)
        dialog = self.dialog()
        dialog.edit_url.setText("https://example.com/other")
        dialog.reject()
        self.assertEqual(self.store.get_pref("notify_webhook"), SLACK_URL)

    def test_clearing_the_field_turns_it_off(self):
        self.store.set_pref("notify_webhook", SLACK_URL)
        dialog = self.dialog()
        dialog.edit_url.setText("")
        dialog.accept()
        self.assertEqual(self.store.get_pref("notify_webhook"), "")

    def test_the_setting_survives_a_restart(self):
        dialog = self.dialog()
        dialog.edit_url.setText(SLACK_URL)
        dialog.accept()
        self.assertEqual(JobStore(self.tmp).get_pref("notify_webhook"), SLACK_URL)


if __name__ == "__main__":
    unittest.main()
