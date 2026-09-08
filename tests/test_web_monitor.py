"""The read-only web view: what it serves, what it refuses, and where it binds.

No Qt here on purpose -- web_monitor.py is deliberately Qt-free so the CI job
that installs only pytest still covers the part with a socket in it.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.error
import urllib.request

from job_manager import web_monitor


SAMPLE = {
    "hosts": [
        {
            "name": "myhost",
            "summary": "CPU 1.20, 8 cores",
            "error": "",
            "load_fraction": 0.25,
            "memory_fraction": 0.5,
            "load_detail": "1.20",
            "memory_detail": "4.0/8.0 GB",
            "jobs": [{"name": "myjob", "state": "RUNNING"}],
        }
    ],
    "generated": "12:00:00",
}


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.server = web_monitor.WebMonitorServer()
        self.port = self.server.start(0)
        self.addCleanup(self.server.stop)
        self.server.publish(SAMPLE)

    def get(self, path="/", token=None, cookie=None, timeout=5):
        url = f"http://127.0.0.1:{self.port}{path}"
        if token is not None:
            url += f"?token={token}"
        request = urllib.request.Request(url)
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, reply.read(), dict(reply.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)


class TestItRefusesWithoutTheToken(ServerTestCase):
    def test_no_token_is_401(self):
        self.assertEqual(self.get("/")[0], 401)

    def test_a_wrong_token_is_401(self):
        self.assertEqual(self.get("/", token="not-the-token")[0], 401)

    def test_the_status_route_is_guarded_too(self):
        # The page is harmless without data; the data is the part worth a check.
        self.assertEqual(self.get("/api/status")[0], 401)

    def test_a_refusal_does_not_leak_the_secret(self):
        _, body, _ = self.get("/")
        self.assertNotIn(self.server.token.encode(), body)


class TestWhatItServes(ServerTestCase):
    def test_the_page_comes_back_whole(self):
        status, body, _ = self.get("/", token=self.server.token)
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body)
        self.assertIn(b"Host Monitor", body)

    def test_the_page_needs_nothing_from_the_network(self):
        # It is opened from a phone on a flaky connection; a stylesheet or a
        # script fetched from a CDN would be a blank screen exactly then. The
        # Content-Security-Policy also forbids it, so a later edit that adds
        # one would fail silently in the browser rather than here.
        _, body, headers = self.get("/", token=self.server.token)
        text = body.decode()
        self.assertNotIn("http://", text.split("<script")[0].replace('xmlns="http://', ""))
        self.assertNotIn("src=", text)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_the_status_route_is_the_published_snapshot(self):
        status, body, _ = self.get("/api/status", token=self.server.token)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["hosts"][0]["name"], "myhost")
        self.assertEqual(payload["hosts"][0]["jobs"][0]["state"], "RUNNING")

    def test_a_later_snapshot_replaces_the_earlier_one(self):
        self.server.publish({"hosts": [], "generated": "13:00:00"})
        _, body, _ = self.get("/api/status", token=self.server.token)
        self.assertEqual(json.loads(body)["hosts"], [])

    def test_an_unknown_path_is_404_not_the_page(self):
        self.assertEqual(self.get("/../secrets", token=self.server.token)[0], 404)

    def test_nothing_is_cached(self):
        _, _, headers = self.get("/", token=self.server.token)
        self.assertEqual(headers["Cache-Control"], "no-store")


class TestTheCookie(ServerTestCase):
    def test_the_first_load_leaves_one(self):
        _, _, headers = self.get("/", token=self.server.token)
        self.assertIn(web_monitor.COOKIE_NAME, headers["Set-Cookie"])

    def test_it_is_accepted_in_place_of_the_query(self):
        # Which is what makes a reload work after the token leaves the address
        # bar, and what makes a phone's reopened tab work tomorrow.
        cookie = f"{web_monitor.COOKIE_NAME}={self.server.token}"
        self.assertEqual(self.get("/", cookie=cookie)[0], 200)

    def test_a_forged_cookie_is_still_refused(self):
        cookie = f"{web_monitor.COOKIE_NAME}=guessed"
        self.assertEqual(self.get("/", cookie=cookie)[0], 401)

    def test_it_is_not_reachable_from_a_page_on_another_site(self):
        # HttpOnly keeps script out of it; SameSite keeps the browser from
        # attaching it to a request some other page made.
        _, _, headers = self.get("/", token=self.server.token)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])


class TestThereIsNoWayToChangeAnything(ServerTestCase):
    """Read-only is the security story; these hold it."""

    def send(self, method):
        url = f"http://127.0.0.1:{self.port}/?token={self.server.token}"
        request = urllib.request.Request(url, data=b"{}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                return reply.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_post_put_and_delete_are_all_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertIn(self.send(method), (405, 501), method)

    def test_the_handler_offers_no_write_route(self):
        for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
            self.assertFalse(hasattr(web_monitor._Handler, verb), verb)


class TestWhereItBinds(unittest.TestCase):
    def test_it_listens_on_loopback_only(self):
        """The whole design rests on this: exposure is tailscale serve's job.

        A bind to 0.0.0.0 would put a machine's host names and job list on
        every network it is attached to, which is exactly what routing the
        exposure through Tailscale is meant to avoid.
        """
        server = web_monitor.WebMonitorServer()
        port = server.start(0)
        self.addCleanup(server.stop)
        self.assertEqual(server._server.server_address[0], "127.0.0.1")

        # And prove it by connecting, not by re-reading the constant: reaching
        # the same port over this machine's routable address must be refused.
        # (Binding that address instead does *not* prove it -- under a 0.0.0.0
        # listener the bind still succeeds on Windows, so that check passed
        # either way and proved nothing. Connecting distinguishes them.)
        try:
            routable = socket.gethostbyname(socket.gethostname())
        except OSError as exc:  # pragma: no cover - depends on the runner
            self.skipTest(f"no routable address to test against: {exc}")
        if routable.startswith("127."):  # pragma: no cover - runner-dependent
            self.skipTest("this machine resolves only to loopback")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(3)
        self.addCleanup(probe.close)
        with self.assertRaises(OSError):
            probe.connect((routable, port))

    def test_a_taken_port_falls_back_rather_than_refusing_to_start(self):
        first = web_monitor.WebMonitorServer()
        port = first.start(0)
        self.addCleanup(first.stop)
        second = web_monitor.WebMonitorServer()
        self.addCleanup(second.stop)
        self.assertNotEqual(second.start(port), port)
        self.assertTrue(second.running)


class TestLifetime(unittest.TestCase):
    def test_stop_releases_the_port_and_the_thread(self):
        server = web_monitor.WebMonitorServer()
        port = server.start(0)
        server.stop()
        self.assertFalse(server.running)
        self.assertEqual(server.port, 0)
        # Bindable again: a socket left open would make the next open of the
        # window fall back to a different port than the copied command names.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(probe.close)
        probe.bind(("127.0.0.1", port))
        self.assertNotIn("job-manager-web-monitor", [t.name for t in threading.enumerate()])

    def test_stopping_twice_is_not_an_error(self):
        server = web_monitor.WebMonitorServer()
        server.start(0)
        server.stop()
        server.stop()

    def test_starting_twice_keeps_the_first_port(self):
        server = web_monitor.WebMonitorServer()
        self.addCleanup(server.stop)
        port = server.start(0)
        self.assertEqual(server.start(0), port)


class TestWhatTheDialogShows(unittest.TestCase):
    def test_the_command_names_the_port_actually_bound(self):
        # Not DEFAULT_PORT: when the default is taken the server moves, and a
        # command still naming the old number would publish someone else's.
        self.assertEqual(web_monitor.tailscale_command(9999), "tailscale serve --bg 9999")

    def test_the_url_carries_the_token(self):
        server = web_monitor.WebMonitorServer()
        self.addCleanup(server.stop)
        server.start(0)
        self.assertIn(f"token={server.token}", server.url())
        self.assertIn("127.0.0.1", server.url())

    def test_there_is_no_url_before_it_is_serving(self):
        self.assertEqual(web_monitor.WebMonitorServer().url(), "")

    def test_the_tailnet_url_is_https(self):
        server = web_monitor.WebMonitorServer()
        self.assertTrue(server.tailscale_url("box.tail1.ts.net").startswith("https://"))

    def test_two_servers_do_not_share_a_token(self):
        self.assertNotEqual(
            web_monitor.WebMonitorServer().token, web_monitor.WebMonitorServer().token
        )


if __name__ == "__main__":
    unittest.main()
