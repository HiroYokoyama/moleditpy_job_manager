"""The API over a real socket: authentication, marshalling and the round trip.

Everything here goes through ``urllib`` against a server that is actually
listening, because the parts most likely to break -- the token check, the hop
onto the GUI thread, a deferred reply -- do not exist at the level
``test_api_core`` works at.
"""

import json
import os
import socket
import tempfile
import unittest
import urllib.error
import urllib.request

import pytest

pytest.importorskip("PyQt6.QtCore", reason="PyQt6 is not installed")

from job_manager import api_core  # noqa: E402
from job_manager.api_server import JobApiServer, port_is_free  # noqa: E402
from job_manager.models import STATE_DONE, STATE_RUNNING, HostProfile, Job  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

from .api_support import FakeService, in_thread, when_ready  # noqa: E402


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="jm_api_server_")
        self.store = JobStore(self.directory)
        self.host = self.store.add_host(HostProfile(name="mycluster", scheduler="slurm"))
        self.service = FakeService(self.store)
        self.server = JobApiServer(self.service)
        # 0: the OS picks a free port, so a developer already running
        # MoleditPy does not have the suite fight it for 8765.
        self.port = self.server.start(0)
        self.addCleanup(self.server.stop)
        handle, self.input_path = tempfile.mkstemp(suffix=".inp", dir=self.directory)
        os.close(handle)

    # --- helpers ------------------------------------------------------------

    def call(self, method, path, body=None, token=None, headers=None, timeout=15.0):
        """``(status, payload)`` for one request, made off the main thread."""
        url = f"{self.server.url()}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        presented = self.server.token() if token is None else token
        if presented:
            request.add_header("Authorization", f"Bearer {presented}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        def fetch():
            try:
                with urllib.request.urlopen(request, timeout=10) as reply:
                    return reply.status, json.loads(reply.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        return in_thread(fetch, timeout=timeout)

    def add_job(self, **fields):
        job = Job(host_id=self.host.id, host_name=self.host.name, **fields)
        self.store.add_job(job)
        return job

    def submit_body(self, **overrides):
        body = {
            "host": "mycluster",
            "files": [self.input_path],
            "command": "mycommand {input} > {stem}.out",
        }
        body.update(overrides)
        return body


class TestListening(ServerTestCase):
    def test_it_listens_on_loopback_and_nowhere_else(self):
        self.assertEqual(self.server._server.server_address[0], "127.0.0.1")
        self.assertTrue(self.server.url().startswith("http://127.0.0.1:"))

    def test_the_endpoint_file_appears_while_it_runs_and_goes_when_it_stops(self):
        path = api_core.endpoint_path(self.directory)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["port"], self.port)
        self.assertEqual(data["token"], self.server.token())
        self.server.stop()
        self.assertFalse(os.path.exists(path))

    def test_stopping_twice_is_harmless(self):
        self.server.stop()
        self.server.stop()
        self.assertFalse(self.server.running)

    def test_a_busy_port_falls_back_to_a_free_one_rather_than_failing(self):
        # A second MoleditPy, or anything else on 8765, must not leave the
        # user with an API that silently refused to start.
        squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        taken = squatter.getsockname()[1]
        self.addCleanup(squatter.close)
        second = JobApiServer(self.service)
        self.addCleanup(second.stop)
        bound = second.start(taken)
        self.assertTrue(bound)
        self.assertNotEqual(bound, taken)

    def test_port_is_free_answers_for_a_port_in_use(self):
        self.assertFalse(port_is_free(self.port))
        self.assertTrue(port_is_free(0))

    def test_starting_twice_keeps_the_first_port(self):
        self.assertEqual(self.server.start(0), self.port)


class TestAuthentication(ServerTestCase):
    def test_no_token_is_a_401_that_says_how_to_send_one(self):
        status, payload = self.call("GET", "/ping", token="")
        self.assertEqual(status, 401)
        self.assertIn("Authorization", payload["error"])

    def test_a_wrong_token_is_a_401(self):
        status, _ = self.call("GET", "/ping", token="not-the-token")
        self.assertEqual(status, 401)

    def test_the_right_token_is_served(self):
        status, payload = self.call("GET", "/ping")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_the_token_may_come_in_the_plugins_own_header(self):
        status, _ = self.call(
            "GET", "/ping", token="", headers={"X-Job-Manager-Token": self.server.token()}
        )
        self.assertEqual(status, 200)

    def test_a_renewed_token_stops_the_old_one_working(self):
        old = self.server.token()
        new = self.server.renew_token()
        self.assertNotEqual(old, new)
        self.assertEqual(self.call("GET", "/ping", token=old)[0], 401)
        self.assertEqual(self.call("GET", "/ping", token=new)[0], 200)

    def test_a_renewed_token_is_republished_for_clients_to_find(self):
        new = self.server.renew_token()
        with open(api_core.endpoint_path(self.directory), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["token"], new)

    def test_a_request_carrying_a_web_pages_origin_is_refused(self):
        # A page on any site can POST to 127.0.0.1 from the user's browser.
        # It cannot read the token, but a blind submission would still be one.
        status, _ = self.call(
            "POST", "/jobs", body=self.submit_body(), headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.service.submitted, [])

    def test_an_origin_on_this_machine_is_allowed(self):
        status, _ = self.call("GET", "/ping", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 200)


class TestTheRoundTrip(ServerTestCase):
    def test_a_submission_over_http_creates_a_tracked_job(self):
        status, payload = self.call("POST", "/jobs", body=self.submit_body(name="water"))
        self.assertEqual(status, 202)
        self.assertEqual(payload["job"]["name"], "water")
        self.assertIn(payload["job"]["id"], self.store.jobs)
        self.assertEqual(self.service.submitted[0][3], [self.input_path])

    def test_a_bad_submission_is_a_400_with_a_sentence_not_a_traceback(self):
        status, payload = self.call("POST", "/jobs", body=self.submit_body(host="nowhere"))
        self.assertEqual(status, 404)
        self.assertIn("mycluster", payload["error"])
        self.assertNotIn("Traceback", payload["error"])

    def test_the_job_list_comes_back_over_http(self):
        self.add_job(name="one", state=STATE_RUNNING)
        status, payload = self.call("GET", "/jobs?state=ACTIVE")
        self.assertEqual(status, 200)
        self.assertEqual([job["name"] for job in payload["jobs"]], ["one"])

    def test_a_deferred_reply_waits_for_the_host_without_freezing_the_gui(self):
        job = self.add_job(name="j", state=STATE_RUNNING, log_file="job.log")

        # Answers from the GUI thread once the handler has actually asked for
        # a tail, exactly as the real one does: if the GUI thread were blocked
        # on the request this would never run and the test would time out.
        # Waiting for the callback rather than for 50ms is what keeps that a
        # statement about the GUI thread instead of about the runner's load.
        when_ready(
            lambda: self.service._tail_done,
            lambda: self.service._tail_done("...tail..."),
        )
        status, payload = self.call("GET", f"/jobs/{job.id}/log?lines=10")
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "...tail...")
        self.assertEqual(self.service.tails[0][2], 10)

    def test_deleting_a_finished_job_removes_it(self):
        job = self.add_job(name="j", state=STATE_DONE)
        status, _ = self.call("DELETE", f"/jobs/{job.id}")
        self.assertEqual(status, 200)
        self.assertNotIn(job.id, self.store.jobs)


class TestMalformedRequests(ServerTestCase):
    def test_a_body_that_is_not_json_is_a_400(self):
        url = f"{self.server.url()}/jobs"
        request = urllib.request.Request(url, data=b"{not json", method="POST")
        request.add_header("Authorization", f"Bearer {self.server.token()}")

        def fetch():
            try:
                with urllib.request.urlopen(request, timeout=10) as reply:
                    return reply.status
            except urllib.error.HTTPError as exc:
                return exc.code

        self.assertEqual(in_thread(fetch), 400)

    def test_a_json_body_that_is_not_an_object_is_a_400(self):
        status, payload = self.call("POST", "/jobs", body=["not", "an", "object"])
        self.assertEqual(status, 400)
        self.assertIn("object", payload["error"])

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        from job_manager.api_server import MAX_BODY_BYTES

        status, _ = self.call("POST", "/jobs", body={"name": "x" * (MAX_BODY_BYTES + 10)})
        self.assertEqual(status, 413)

    def test_an_unknown_path_is_a_404_and_the_server_stays_up(self):
        self.assertEqual(self.call("GET", "/nowhere")[0], 404)
        self.assertEqual(self.call("GET", "/ping")[0], 200)


if __name__ == "__main__":
    unittest.main()


class TestARefusalStillReachesTheClient(ServerTestCase):
    """A refusal decided before the body is read has to drain it first.

    Origin and token are checked before _body() runs, so replying immediately
    leaves the client writing into a socket the server is closing. With a
    small body it fits the socket buffer and nothing shows; past that the
    client gets a reset instead of the JSON saying what was wrong -- on
    Windows a ConnectionAbortedError, which is how this surfaced in CI.
    """

    #: Comfortably past a socket buffer, so the race is certain rather than
    #: occasional. Under MAX_BODY_BYTES so nothing else refuses it first.
    BIG = 400_000

    def big_body(self):
        return self.submit_body(pad="p" * self.BIG)

    def test_a_bad_token_is_answered_not_dropped(self):
        status, payload = self.call("POST", "/jobs", body=self.big_body(), token="wrong")
        self.assertEqual(status, 401)
        self.assertIn("token", payload["error"].lower())

    def test_a_refused_origin_is_answered_not_dropped(self):
        status, payload = self.call(
            "POST",
            "/jobs",
            body=self.big_body(),
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.service.submitted, [])

    def test_a_small_refused_body_still_works(self):
        # The case that always passed; kept so the fix cannot regress it.
        status, _ = self.call(
            "POST", "/jobs", body=self.submit_body(), headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
