"""The read-only web view: what it serves, what it refuses, and where it binds.

No Qt here on purpose -- web_monitor.py is deliberately Qt-free so the CI job
that installs only pytest still covers the part with a socket in it.
"""

from __future__ import annotations

import json
import socket
import threading
import unittest
import unittest.mock
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
        # No body. BaseHTTPRequestHandler answers 501 without reading one and
        # then closes, so a request that is still writing gets the connection
        # shut under it -- WinError 10053, intermittently, on the very test
        # that is supposed to prove the method was refused.
        request = urllib.request.Request(url, method=method)
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


class TestRunningTheTailscaleCommand(unittest.TestCase):
    """The Run button's half: what is executed, and what a failure reports.

    The CLI is never actually invoked here -- a test that published this
    machine to a real tailnet would be a test with a side effect on the
    developer's network.
    """

    def _fake_run(self, returncode=0, stdout="", stderr=""):
        calls = []

        class Done:
            pass

        def run(args, **kwargs):
            calls.append((args, kwargs))
            done = Done()
            done.returncode, done.stdout, done.stderr = returncode, stdout, stderr
            return done

        return run, calls

    def test_it_serves_the_port_it_was_given(self):
        run, calls = self._fake_run()
        with unittest.mock.patch.object(
            web_monitor.shutil, "which", return_value="/usr/bin/tailscale"
        ):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                ok, _ = web_monitor.serve_on_tailnet(8770)
        self.assertTrue(ok)
        # Not calls[0]: the readiness check runs first now, so the serve call
        # is asserted by presence rather than by position.
        self.assertIn(["/usr/bin/tailscale", "serve", "--bg", "8770"], [c[0] for c in calls])

    def test_the_command_is_a_list_so_no_shell_re_splits_it(self):
        run, calls = self._fake_run()
        with unittest.mock.patch.object(
            web_monitor.shutil, "which", return_value="/usr/bin/tailscale"
        ):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                web_monitor.serve_on_tailnet(8770)
        self.assertIsInstance(calls[0][0], list)
        self.assertNotIn("shell", calls[0][1])

    def test_unpublishing_resets_rather_than_guessing_a_port(self):
        run, calls = self._fake_run()
        with unittest.mock.patch.object(
            web_monitor.shutil, "which", return_value="/usr/bin/tailscale"
        ):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                web_monitor.stop_serving_on_tailnet()
        self.assertEqual(calls[0][0][1:], ["serve", "reset"])

    def test_tailscales_own_error_is_what_reaches_the_user(self):
        # "not logged in" and "HTTPS is not enabled for this tailnet" are the
        # two real ones, and neither is improved by being paraphrased.
        run, _ = self._fake_run(returncode=1, stderr="needs HTTPS enabled")
        with unittest.mock.patch.object(
            web_monitor.shutil, "which", return_value="/usr/bin/tailscale"
        ):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                ok, message = web_monitor.serve_on_tailnet(8770)
        self.assertFalse(ok)
        self.assertIn("HTTPS", message)

    def test_a_missing_cli_is_reported_not_raised(self):
        with unittest.mock.patch.object(web_monitor.shutil, "which", return_value=None):
            ok, message = web_monitor.serve_on_tailnet(8770)
        self.assertFalse(ok)
        self.assertIn("not found", message)

    def test_a_hang_is_reported_not_waited_out(self):
        def run(args, **kwargs):
            raise web_monitor.subprocess.TimeoutExpired(args, 20)

        with unittest.mock.patch.object(
            web_monitor.shutil, "which", return_value="/usr/bin/tailscale"
        ):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                ok, message = web_monitor.serve_on_tailnet(8770)
        self.assertFalse(ok)
        self.assertIn("time", message.lower())


class TestTheTailnetName(unittest.TestCase):
    def _status(self, payload):
        def run(args, **kwargs):
            class Done:
                returncode, stdout, stderr = 0, json.dumps(payload), ""

            return Done()

        return run

    def test_the_machines_own_name_is_read_and_the_trailing_dot_dropped(self):
        # DNSName comes back fully qualified with a trailing dot, which is not
        # what anyone types into a browser.
        run = self._status({"Self": {"DNSName": "mybox.tail1234.ts.net."}})
        with unittest.mock.patch.object(
            web_monitor.shutil, "which", return_value="/usr/bin/tailscale"
        ):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                self.assertEqual(web_monitor.tailscale_dns_name(), "mybox.tail1234.ts.net")

    def test_nonsense_output_is_an_empty_name_not_a_crash(self):
        def run(args, **kwargs):
            class Done:
                returncode, stdout, stderr = 0, "not json at all", ""

            return Done()

        with unittest.mock.patch.object(
            web_monitor.shutil, "which", return_value="/usr/bin/tailscale"
        ):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                self.assertEqual(web_monitor.tailscale_dns_name(), "")

    def test_a_name_makes_a_link_that_needs_no_hand_editing(self):
        server = web_monitor.WebMonitorServer()
        url = server.tailscale_url("mybox.tail1234.ts.net")
        self.assertNotIn("<", url)
        self.assertIn(server.token, url)


class TestThePageCanActuallyReachItsOwnData(ServerTestCase):
    """The regression behind "reconnecting..." for ever.

    connect-src falls back to default-src, so `default-src 'none'` blocked the
    page's own fetch of /api/status. The server answered every request
    correctly; the browser never sent one. Asserting the header *contains*
    "default-src 'none'" passed throughout -- it checked the text and not the
    rule -- so these parse the policy and ask the question the browser asks.
    """

    def policy(self):
        _, _, headers = self.get("/", token=self.server.token)
        directives = {}
        for part in headers["Content-Security-Policy"].split(";"):
            name, _, value = part.strip().partition(" ")
            if name:
                directives[name] = value.strip()
        return directives

    def test_the_page_is_allowed_to_fetch_from_its_own_origin(self):
        policy = self.policy()
        effective = policy.get("connect-src", policy.get("default-src", ""))
        self.assertNotEqual(
            effective,
            "'none'",
            "connect-src resolves to 'none', so the page cannot poll /api/status",
        )
        self.assertIn("'self'", effective)

    def test_the_policy_is_still_closed_by_default(self):
        # The fix must not become "allow everything": only connect-src was
        # ever needed.
        self.assertEqual(self.policy().get("default-src"), "'none'")

    def test_the_page_fetches_a_same_origin_path(self):
        # 'self' only helps if the URL really is same-origin; an absolute URL
        # to somewhere else would be blocked again, and silently.
        _, body, _ = self.get("/", token=self.server.token)
        text = body.decode()
        target = text.split('fetch("', 1)[1].split('"', 1)[0]
        self.assertFalse(target.startswith(("http://", "https://", "//")), target)


class TestTailscaleIsAskedBeforeItIsTold(unittest.TestCase):
    """The other half of the report: "Tailscale did not answer in time"."""

    def _status_run(self, payload, serve_result=(0, "", "")):
        calls = []

        def run(args, **kwargs):
            calls.append(args)

            class Done:
                pass

            done = Done()
            if "status" in args:
                done.returncode, done.stdout, done.stderr = 0, json.dumps(payload), ""
            else:
                done.returncode, done.stdout, done.stderr = serve_result
            return done

        return run, calls

    def test_a_tailnet_without_https_is_told_so_instead_of_timing_out(self):
        # CertDomains empty means serve has no certificate to obtain, so it
        # waits -- and a twenty-second pause says nothing about the one
        # setting that fixes it.
        run, calls = self._status_run({"BackendState": "Running", "CertDomains": None})
        with unittest.mock.patch.object(web_monitor.shutil, "which", return_value="/ts"):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                ok, message = web_monitor.serve_on_tailnet(8770)
        self.assertFalse(ok)
        self.assertIn("HTTPS is not enabled", message)
        self.assertIn(web_monitor.HTTPS_HELP_URL, message)
        # And it never got as far as the command that would have hung.
        self.assertTrue(all("serve" not in args for args in calls), calls)

    def test_a_disconnected_tailscale_is_named_as_such(self):
        run, _ = self._status_run({"BackendState": "Stopped", "CertDomains": ["x"]})
        with unittest.mock.patch.object(web_monitor.shutil, "which", return_value="/ts"):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                ok, message = web_monitor.serve_on_tailnet(8770)
        self.assertFalse(ok)
        self.assertIn("not connected", message)

    def test_a_ready_tailnet_goes_on_to_serve(self):
        run, calls = self._status_run(
            {"BackendState": "Running", "CertDomains": ["box.tail1.ts.net"]}
        )
        with unittest.mock.patch.object(web_monitor.shutil, "which", return_value="/ts"):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                ok, _ = web_monitor.serve_on_tailnet(8770)
        self.assertTrue(ok)
        self.assertIn(["/ts", "serve", "--bg", "8770"], calls)

    def test_no_command_can_sit_waiting_on_an_answer_nobody_can_see(self):
        # capture_output hides any prompt, so stdin must be closed or the
        # process blocks until the timeout with no clue why.
        seen = {}

        def run(args, **kwargs):
            seen.update(kwargs)

            class Done:
                returncode, stdout, stderr = 0, "{}", ""

            return Done()

        with unittest.mock.patch.object(web_monitor.shutil, "which", return_value="/ts"):
            with unittest.mock.patch.object(web_monitor.subprocess, "run", run):
                web_monitor.stop_serving_on_tailnet()
        self.assertEqual(seen.get("stdin"), web_monitor.subprocess.DEVNULL)


class TestTheBarsWarnByColour(ServerTestCase):
    """A stressed machine is meant to be obvious without reading the number."""

    def page(self):
        return self.get("/", token=self.server.token)[1].decode()

    def test_it_goes_green_then_yellow_then_red(self):
        page = self.page()
        self.assertIn('pct >= 90 ? "bad"', page)
        self.assertIn('pct >= 70 ? "warn"', page)
        for name in ("--ok:", "--warn:", "--bad:"):
            self.assertIn(name, page)


class TestTheThemeToggle(ServerTestCase):
    """Two states and a button, the way the Plugin Explorer page does it."""

    def page(self):
        return self.get("/", token=self.server.token)[1].decode()

    def test_there_is_a_button_with_both_icons(self):
        page = self.page()
        self.assertIn('id="theme"', page)
        self.assertIn('class="sun"', page)
        self.assertIn('class="moon"', page)

    def test_the_system_preference_only_decides_where_it_starts(self):
        # Read once, at load. A media query on the variables as well would
        # fight the class every time the OS disagreed with the last press.
        page = self.page()
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', page)
        style = page.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertNotIn("prefers-color-scheme", style)

    def test_the_choice_is_remembered(self):
        page = self.page()
        self.assertIn('localStorage.setItem("jm_theme"', page)
        self.assertIn('localStorage.getItem("jm_theme")', page)

    def test_a_stored_choice_beats_the_system_preference(self):
        # Otherwise the button appears to do nothing on the next visit from a
        # device whose OS says the opposite.
        self.assertIn('saved === "dark" || (!saved && prefersDark)', self.page())

    def test_both_schemes_define_every_colour(self):
        # A variable defined only in one block is a missing colour in the
        # other -- worst for the amber and red that are meant to warn.
        page = self.page()
        style = page.split("<style>", 1)[1].split("</style>", 1)[0]
        light = style.split(":root.dark", 1)[0]
        dark = style.split(":root.dark", 1)[1]
        for name in ("--ok:", "--warn:", "--bad:", "--bg:", "--text:", "--track:", "--card:"):
            self.assertIn(name, light, f"{name} missing from light")
            self.assertIn(name, dark, f"{name} missing from dark")

    def test_storage_being_unavailable_does_not_stop_the_toggle(self):
        self.assertIn('try { localStorage.setItem("jm_theme"', self.page())


class TestTheFooterNamesTheVersion(ServerTestCase):
    def test_the_snapshot_carries_it(self):
        from job_manager import PLUGIN_VERSION

        _, body, _ = self.get("/api/status", token=self.server.token)
        self.assertEqual(json.loads(body)["version"], PLUGIN_VERSION)

    def test_the_page_shows_what_the_server_reports(self):
        # Read from the response rather than baked into the HTML: a browser
        # holding a cached page would otherwise name the version it was built
        # with, not the one answering.
        page = self.get("/", token=self.server.token)[1].decode()
        self.assertIn("d.version", page)

    def test_publishing_a_snapshot_cannot_overwrite_it(self):
        # The publisher is the GUI thread, which has no business deciding what
        # version the server answering the request is.
        from job_manager import PLUGIN_VERSION

        self.server.publish({"hosts": [], "generated": "x", "version": "9.9.9"})
        self.assertEqual(self.server.snapshot()["version"], PLUGIN_VERSION)


class TestTheRefreshIntervalIsThePagesToChoose(ServerTestCase):
    """A phone on a metered connection pays for every poll, and only the
    person holding it knows whether the tab is watched or left open."""

    def page(self):
        return self.get("/", token=self.server.token)[1].decode()

    def test_the_choice_is_offered_on_the_page(self):
        page = self.page()
        self.assertIn('id="every"', page)
        for seconds in ("2", "4", "10", "30", "60", "300"):
            self.assertIn(f'value="{seconds}"', page)

    def test_pausing_is_one_of_the_choices(self):
        # The cheapest setting of all, and the honest one for a tab someone
        # leaves open on a train.
        page = self.page()
        self.assertIn('value="0"', page)
        self.assertIn("Paused", page)

    def test_zero_stops_the_timer_rather_than_polling_immediately(self):
        # setInterval(fn, 0) is not "off" -- it is as fast as the browser will
        # run it, which on a metered link is the opposite of what was asked.
        page = self.page()
        self.assertIn("if (seconds > 0)", page)
        self.assertIn("clearInterval", page)

    def test_the_choice_survives_a_reload(self):
        page = self.page()
        self.assertIn('localStorage.setItem("jm_every"', page)
        self.assertIn('localStorage.getItem("jm_every")', page)

    def test_a_stored_value_the_page_no_longer_offers_is_ignored(self):
        # Otherwise a select with no matching option shows blank, and the
        # timer is never scheduled at all.
        self.assertIn("every.options].some", self.page())

    def test_storage_being_unavailable_is_not_fatal(self):
        # Private browsing on iOS throws on localStorage rather than returning
        # null, and an uncaught error there would leave the page never polling.
        page = self.page()
        self.assertIn("try { localStorage.setItem", page)
        self.assertGreaterEqual(page.count("catch (e) {}"), 2)


class TestTheTokenOutlivesTheWindow(unittest.TestCase):
    """The link is meant to be saved on a phone.

    It used to be minted per WebMonitorServer, so closing and reopening the
    Host Monitor invalidated every bookmark and cookie already handed out --
    while the server itself came back on its own, so nothing looked wrong
    from the desktop side.
    """

    def setUp(self):
        import shutil as _shutil
        import tempfile

        self.directory = tempfile.mkdtemp(prefix="jm_webtoken_")
        self.addCleanup(_shutil.rmtree, self.directory, True)

    def test_it_is_the_same_across_sessions(self):
        first = web_monitor.ensure_web_token(self.directory)
        self.assertEqual(web_monitor.ensure_web_token(self.directory), first)

    def test_renewing_replaces_it(self):
        first = web_monitor.ensure_web_token(self.directory)
        self.assertNotEqual(web_monitor.ensure_web_token(self.directory, renew=True), first)

    def test_it_is_not_the_api_token(self):
        # That one grants full control; this page is read-only, and pasting a
        # full-control secret into a phone's browser history is not the price
        # of looking at a load average.
        from job_manager import api_core

        self.assertNotEqual(
            web_monitor.ensure_web_token(self.directory),
            api_core.ensure_token(self.directory),
        )
        self.assertNotEqual(
            web_monitor.web_token_path(self.directory), api_core.token_path(self.directory)
        )

    def test_it_is_written_where_only_this_user_can_read_it(self):
        import os
        import stat

        web_monitor.ensure_web_token(self.directory)
        mode = os.stat(web_monitor.web_token_path(self.directory)).st_mode
        if os.name == "nt":  # pragma: no cover - POSIX bits are not meaningful here
            self.skipTest("file modes are an ACL matter on Windows")
        self.assertFalse(mode & (stat.S_IRGRP | stat.S_IROTH))

    def test_a_running_server_can_be_handed_a_new_one(self):
        server = web_monitor.WebMonitorServer("first-token")
        self.addCleanup(server.stop)
        server.start(0)
        server.set_token("second-token")
        self.assertEqual(server.token, "second-token")
        self.assertIn("second-token", server.url())

    def test_the_old_link_stops_working_once_it_is_renewed(self):
        server = web_monitor.WebMonitorServer("first-token")
        port = server.start(0)
        self.addCleanup(server.stop)
        server.set_token("second-token")

        def status(token):
            request = urllib.request.Request(f"http://127.0.0.1:{port}/?token={token}")
            try:
                with urllib.request.urlopen(request, timeout=5) as reply:
                    return reply.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(status("second-token"), 200)
        self.assertEqual(status("first-token"), 401)


class TestTheHeaderControlsStayPut(ServerTestCase):
    """They were laid out so that "Refresh" appeared to name the theme button,
    and on a phone the group split across two rows and shifted as the reading
    changed width."""

    def page(self):
        return self.get("/", token=self.server.token)[1].decode()

    def test_the_label_sits_with_the_control_it_names(self):
        import re

        header = self.page().split("<header>", 1)[1].split("</header>", 1)[0]
        order = re.findall(r'id="theme"|for="every"|id="every"', header)
        self.assertEqual(order, ['id="theme"', 'for="every"', 'id="every"'])

    def test_they_are_one_group(self):
        self.assertIn('class="controls"', self.page())

    def test_the_group_never_splits_across_lines(self):
        page = self.page()
        style = page.split("<style>", 1)[1].split("</style>", 1)[0]
        controls = style.split(".controls", 1)[1].split("}", 1)[0]
        self.assertIn("flex-wrap:nowrap", controls)
        self.assertIn("white-space:nowrap", controls)

    def test_it_always_gets_a_row_of_its_own(self):
        # Not "beside the title when there is room": that made the controls
        # jump between the first line and the second as the clock text
        # changed length, which on a phone is under the thumb reaching for
        # them. A full-width basis pins the row at every width.
        style = self.page().split("<style>", 1)[1].split("</style>", 1)[0]
        controls = style.split(".controls", 1)[1].split("}", 1)[0]
        self.assertIn("flex:0 0 100%", controls)

    def test_the_clock_gives_way_rather_than_the_controls(self):
        # #age grows and truncates; the controls are fixed. The other way
        # round, a longer timestamp would push them off the line.
        style = self.page().split("<style>", 1)[1].split("</style>", 1)[0]
        age = style.split("#age", 1)[1].split("}", 1)[0]
        self.assertIn("text-overflow:ellipsis", age)
        self.assertIn("min-width:0", age)
        controls = style.split(".controls", 1)[1].split("}", 1)[0]
        self.assertIn("flex:0 0 100%", controls)

    def test_nothing_references_the_removed_spacer(self):
        page = self.page()
        self.assertNotIn('class="spacer"', page)
        self.assertNotIn(".spacer", page)


class TestThePageHasAnIcon(ServerTestCase):
    """A browser tab with no icon is hard to find among twenty others."""

    def page(self):
        return self.get("/", token=self.server.token)[1].decode()

    def test_the_link_is_there_and_filled_in(self):
        page = self.page()
        self.assertIn('<link rel="icon"', page)
        self.assertNotIn("__ICON__", page)

    def test_it_is_fetched_from_a_route(self):
        # Inline was tried first, on the reasoning that a route would be one
        # more thing to authorise. It is -- but Safari ignores a data: favicon
        # and iOS refuses one as an apple-touch-icon, so inlining bought a
        # tidy page and an icon that appeared in Chrome only.
        self.assertIn('href="icon.svg"', self.page())

    def test_the_policy_allows_it(self):
        # default-src 'none' covers img-src too, so without an explicit
        # allowance the browser blocks the icon and the tab stays blank --
        # exactly the way it blocked the page's own fetch once already.
        _, _, headers = self.get("/", token=self.server.token)
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])

    def test_it_carries_the_apps_own_accent(self):
        # An icon that looked like nothing else in MoleditPy would be worse
        # than none: the bottom unit is the application's blue.
        svg = web_monitor.FAVICON_SVG
        self.assertIn("#1e5fd0", svg)
        self.assertIn("<circle", svg)

    def test_the_indicator_dots_are_oversized_on_purpose(self):
        # It is a 16 px drawing before it is anything else. A realistic LED is
        # one pale pixel at tab size, and the icon collapses into three grey
        # bars; r=2 against a 6.5-high unit is what keeps them visible.
        svg = web_monitor.FAVICON_SVG
        self.assertEqual(svg.count('r="2"'), 3)
        self.assertIn('fill="#00e676"', svg)

    def test_it_reads_as_a_stack_of_machines(self):
        # Three units, not one box: the plugin is about several hosts.
        self.assertEqual(web_monitor.FAVICON_SVG.count('rx="2.2"'), 3)

    def test_it_is_readable_on_a_dark_tab(self):
        # The needle is near-black, so a transparent icon would be a hole with
        # an arc in it on dark browser chrome.
        self.assertIn('<rect width="32" height="32"', web_monitor.FAVICON_SVG)
        self.assertIn('fill="#ffffff"', web_monitor.FAVICON_SVG)


class TestTheIconReachesEveryBrowser(ServerTestCase):
    """It has to be a URL, not a payload.

    Inlining the icon as a data: URI worked in Chrome and nowhere else: Safari
    ignores a data: favicon entirely, and iOS will not accept one as an
    apple-touch-icon at all -- so the icon was missing on precisely the device
    this page exists to be read from.
    """

    def page(self):
        return self.get("/", token=self.server.token)[1].decode()

    def test_the_page_points_at_urls_not_payloads(self):
        page = self.page()
        head = page.split("</head>", 1)[0]
        self.assertIn('href="icon.svg"', head)
        self.assertIn('href="icon.png"', head)
        self.assertIn('href="apple-touch-icon.png"', head)
        self.assertNotIn("data:image", head)

    def test_every_icon_is_reachable_without_the_token(self):
        # A browser fetching an icon need not send the cookie, and an
        # apple-touch-icon fetch in particular does not. Gating these would
        # 401 them and show nothing, which is the whole failure being fixed.
        for path in sorted(web_monitor.ICON_ROUTES):
            status, body, headers = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body, path)
            self.assertTrue(headers["Content-Type"].startswith("image/"), path)

    def test_ios_probes_the_root_and_finds_something(self):
        # iOS asks for these whether or not the page names them.
        for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            self.assertEqual(self.get(path)[0], 200, path)

    def test_opening_the_icons_did_not_open_anything_else(self):
        # The icons are public because the drawing is; nothing else may be.
        for path in ("/", "/api/status", "/anything"):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_the_policy_allows_them_from_this_origin(self):
        _, _, headers = self.get("/", token=self.server.token)
        self.assertIn("img-src 'self'", headers["Content-Security-Policy"])

    def test_the_svg_route_is_served_as_svg(self):
        # A PNG body under an image/svg+xml type, or the reverse, renders as
        # nothing; nosniff means the browser will not rescue it either.
        _, body, headers = self.get("/icon.svg")
        self.assertEqual(headers["Content-Type"], "image/svg+xml")
        self.assertTrue(body.startswith(b"<svg"))

    def test_the_pngs_are_real_pngs(self):
        import base64

        for b64 in (web_monitor.FAVICON_PNG_B64, web_monitor.TOUCH_ICON_PNG_B64):
            self.assertTrue(base64.b64decode(b64).startswith(b"\x89PNG\r\n\x1a\n"))
