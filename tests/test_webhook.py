"""Posting a finished job to a chat room.

No Qt and no network: the payload a service will accept is decided by a pure
function, and the one place that opens a socket is patched. The interesting
part is that the wrong payload is silently accepted by nobody -- Discord reads
``content`` and rejects fields it does not know, Slack reads ``text`` -- so a
URL going to the wrong branch means an alert that never arrives, hours later,
with nothing to see.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from job_manager import webhook
from job_manager.store import JobStore

SLACK_URL = "https://hooks.slack.com/services/T000/B000/xxxx"
DISCORD_URL = "https://discord.com/api/webhooks/123/abcdef"
TEAMS_URL = "https://example.webhook.office.com/webhookb2/abc"


class TestWhichServiceItIs(unittest.TestCase):
    def test_slack(self):
        self.assertEqual(webhook.flavour(SLACK_URL), webhook.SLACK)

    def test_discord(self):
        self.assertEqual(webhook.flavour(DISCORD_URL), webhook.DISCORD)

    def test_a_discord_subdomain_is_still_discord(self):
        self.assertEqual(
            webhook.flavour("https://canary.discord.com/api/webhooks/1/x"), webhook.DISCORD
        )

    def test_anything_else_is_generic(self):
        self.assertEqual(webhook.flavour(TEAMS_URL), webhook.GENERIC)

    def test_a_host_that_merely_contains_the_name_is_not_slack(self):
        # "slack.example.com" is somebody else's server, and sending it a
        # Slack-shaped body is at best noise.
        self.assertEqual(webhook.flavour("https://slack.example.com/hook"), webhook.GENERIC)

    def test_the_name_shown_to_the_user(self):
        self.assertEqual(webhook.service_name(DISCORD_URL), "Discord")
        self.assertEqual(webhook.service_name(SLACK_URL), "Slack")


class TestWhatIsSent(unittest.TestCase):
    def test_discord_gets_content(self):
        payload = webhook.build_payload(DISCORD_URL, "title", "body")
        self.assertEqual(list(payload), ["content"])
        self.assertIn("body", payload["content"])

    def test_slack_gets_text(self):
        payload = webhook.build_payload(SLACK_URL, "title", "body")
        self.assertEqual(list(payload), ["text"])

    def test_a_generic_hook_gets_text_without_markup(self):
        payload = webhook.build_payload(TEAMS_URL, "title", "body")
        self.assertEqual(payload, {"text": "title\nbody"})

    def test_only_one_key_ever(self):
        # Both at once is the tempting shortcut, and Discord 400s on it.
        for url in (SLACK_URL, DISCORD_URL, TEAMS_URL):
            self.assertEqual(len(webhook.build_payload(url, "t", "m")), 1, url)


class TestWhatCountsAsAUrl(unittest.TestCase):
    def test_https(self):
        self.assertTrue(webhook.is_supported(SLACK_URL))

    def test_http(self):
        self.assertTrue(webhook.is_supported("http://localhost:8080/hook"))

    def test_empty(self):
        self.assertFalse(webhook.is_supported(""))

    def test_a_local_file_is_refused(self):
        # urlopen will happily open one, and a notification must not be able to
        # read this machine's disk because somebody pasted the wrong thing.
        self.assertFalse(webhook.is_supported("file:///etc/passwd"))

    def test_a_bare_path_is_refused(self):
        self.assertFalse(webhook.is_supported("/tmp/hook"))

    def test_a_scheme_with_no_host_is_refused(self):
        self.assertFalse(webhook.is_supported("https:///hook"))


class _Response:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestThePost(unittest.TestCase):
    def post(self, url=DISCORD_URL, response=None):
        opened = MagicMock(return_value=response or _Response())
        with patch("urllib.request.urlopen", opened):
            ok = webhook.post(url, "title", "body")
        return ok, opened

    def test_it_reports_success(self):
        ok, _opened = self.post()
        self.assertTrue(ok)

    def test_the_body_is_json_for_that_service(self):
        _ok, opened = self.post()
        request = opened.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data.decode()), {"content": "**title**\nbody"})
        self.assertEqual(request.headers["Content-type"], "application/json")

    def test_a_url_that_is_not_one_sends_nothing(self):
        ok, opened = self.post(url="not a url")
        self.assertFalse(ok)
        opened.assert_not_called()

    def test_a_rejected_webhook_is_false_and_not_an_exception(self):
        error = urllib.error.HTTPError(DISCORD_URL, 404, "gone", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertFalse(webhook.post(DISCORD_URL, "t", "m"))

    def test_a_network_that_is_not_there_is_false_and_not_an_exception(self):
        with patch("urllib.request.urlopen", side_effect=OSError("unreachable")):
            self.assertFalse(webhook.post(DISCORD_URL, "t", "m"))

    def test_a_non_2xx_answer_is_false(self):
        ok, _opened = self.post(response=_Response(status=500))
        self.assertFalse(ok)


class TestPostingWithoutWaiting(unittest.TestCase):
    def test_it_runs_off_this_thread_and_finishes(self):
        seen = []
        with patch.object(webhook, "post", side_effect=lambda *a, **k: seen.append(a)):
            thread = webhook.post_async(SLACK_URL, "t", "m")
            self.assertIsNotNone(thread)
            thread.join(5)
        self.assertEqual(seen, [(SLACK_URL, "t", "m")])

    def test_no_thread_is_started_for_an_empty_setting(self):
        # The default. Every finished job comes through here, so this is the
        # path that must cost nothing.
        with patch.object(webhook, "post") as post:
            self.assertIsNone(webhook.post_async("", "t", "m"))
        post.assert_not_called()

    def test_a_daemon_thread_never_holds_the_application_open(self):
        with patch.object(webhook, "post", return_value=True):
            thread = webhook.post_async(SLACK_URL, "t", "m")
            self.assertTrue(thread.daemon)
            thread.join(5)


class TestThePreferenceDefault(unittest.TestCase):
    def test_nothing_is_configured_out_of_the_box(self):
        tmp = tempfile.mkdtemp(prefix="webhook_pref_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertEqual(JobStore(tmp).get_pref("notify_webhook"), "")


if __name__ == "__main__":
    unittest.main()
