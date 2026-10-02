"""The web view's own life: serving, remembering, and sampling on demand."""

from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.request

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from job_manager import web_service  # noqa: E402
from job_manager.models import STATE_DONE, STATE_RUNNING, Job  # noqa: E402

from .test_host_monitor_gui import CountingTransport, HostMonitorTestCase  # noqa: E402


class WebTestCase(HostMonitorTestCase):
    def setUp(self):
        super().setUp()
        self.web = web_service.for_service(self.service)
        self.addCleanup(web_service.shutdown_for, self.service)


class TestServing(WebTestCase):
    def test_starting_serves_and_is_remembered(self):
        self.assertTrue(self.web.start())
        self.assertTrue(self.web.running)
        self.assertTrue(self.store.get_pref("host_monitor_web", False))

    def test_switching_it_off_is_remembered(self):
        self.web.start()
        self.web.stop()
        self.assertFalse(self.web.running)
        self.assertFalse(self.store.get_pref("host_monitor_web", True))

    def test_a_process_ending_keeps_the_choice(self):
        self.web.start()
        web_service.shutdown_for(self.service)
        self.assertFalse(self.web.running)
        self.assertTrue(self.store.get_pref("host_monitor_web", False))

    def test_resume_serves_only_what_was_left_on(self):
        web_service.resume(self.service)
        self.assertFalse(self.web.running)
        self.store.set_pref("host_monitor_web", True)
        web_service.resume(self.service)
        self.assertTrue(self.web.running)

    def test_one_web_view_per_service(self):
        self.assertIs(web_service.for_service(self.service), self.web)


class TestTheSavedPortIsWaitedFor(WebTestCase):
    """The tray process starts while the MoleditPy it replaces still holds the
    port; taking another would break every saved link."""

    def setUp(self):
        super().setUp()
        self.holder = socket.socket()
        self.holder.bind(("127.0.0.1", 0))
        self.holder.listen(1)
        self.addCleanup(self.holder.close)
        self.port = self.holder.getsockname()[1]
        self.store.set_pref("host_monitor_web_port", self.port)

    def test_it_waits_and_takes_the_port_once_free(self):
        self.web.start(wait_for_port=True)
        self.assertFalse(self.web.running)
        self.holder.close()
        self.web._try_exact_port()
        self.assertTrue(self.web.running)
        self.assertEqual(self.web.port, self.port)

    def test_it_settles_for_another_port_in_the_end(self):
        self.web.start(wait_for_port=True)
        self.web._wait_until = 0.0
        self.web._try_exact_port()
        self.assertTrue(self.web.running)
        self.assertNotEqual(self.web.port, self.port)


class TestSamplingOnDemand(WebTestCase):
    """The page can be opened with no window at the desk."""

    def test_nothing_is_sampled_until_someone_looks(self):
        self.web.start()
        self.assertFalse(self.web.sampler.active)
        self.assertEqual(self.transports, {})

    def test_a_request_starts_sampling(self):
        self.web.start()
        self.web._on_request()
        self.assertTrue(self.web.sampler.active)
        self.assertEqual(self.transports[self.host.id].runs, 1)

    def test_it_stops_once_nobody_has_looked_for_a_while(self):
        self.web.start()
        self.web._on_request()
        self.web._idle.timeout.emit()
        self.assertFalse(self.web.sampler.active)
        self.assertEqual(self.transports[self.host.id].closes, 1)

    def test_it_outlasts_the_pages_slowest_refresh(self):
        self.assertGreater(web_service.IDLE_SECONDS, 300)

    def test_a_request_with_nothing_served_samples_nothing(self):
        self.web._on_request()
        self.assertFalse(self.web.sampler.active)

    def test_switching_it_off_stops_the_sampling_it_started(self):
        self.web.start()
        self.web._on_request()
        self.web.stop()
        self.assertFalse(self.web.sampler.active)

    def test_a_real_request_calls_the_hook(self):
        # On the HTTP thread, so the hook is only an event to set here: the
        # service hands it a signal emit, which Qt queues onto the GUI thread.
        self.web.start()
        seen = threading.Event()
        self.web.server._on_request = seen.set
        url = f"http://127.0.0.1:{self.web.port}/api/status?token={self.web.server.token}"
        with urllib.request.urlopen(url, timeout=5) as response:
            json.loads(response.read())
        self.assertTrue(seen.wait(5))

    def test_the_hooks_signal_starts_sampling(self):
        self.web.start()
        self.web._requested.emit()
        self.assertTrue(self.web.sampler.active)


class TestTheSnapshot(WebTestCase):
    def test_it_carries_the_hosts_stats_and_active_jobs(self):
        self.store.add_job(Job(id="live", name="myjob", host_id=self.host.id, state=STATE_RUNNING))
        self.web.start()
        self.web._on_request()
        entry = self.web.snapshot()["hosts"][0]
        self.assertEqual(entry["name"], self.host.name)
        self.assertGreater(entry["load_fraction"], 0)
        self.assertIn("myjob", [job["name"] for job in entry["jobs"]])

    def test_finished_jobs_are_left_out(self):
        self.store.add_job(Job(id="old", name="finished", host_id=self.host.id, state=STATE_DONE))
        names = [j["name"] for j in self.web.snapshot()["hosts"][0]["jobs"]]
        self.assertNotIn("finished", names)

    def test_before_the_first_reading_it_says_so(self):
        # Not a blank card: the first load starts the sampling, so it names
        # the hosts and why there are no numbers yet.
        entry = self.web.snapshot()["hosts"][0]
        self.assertIn("waiting", entry["summary"])

    def test_a_failed_probe_reaches_the_page(self):
        self.transports[self.host.id] = CountingTransport(fail="timed out")
        self.web.start()
        self.web._on_request()
        self.assertIn("timed out", self.web.snapshot()["hosts"][0]["error"])

    def test_it_is_plain_data(self):
        # It crosses onto the HTTP thread, where touching a widget or a
        # transport would be a data race rather than a wrong number.
        self.web._on_request()
        json.dumps(self.web.snapshot())

    def test_what_is_served_follows_each_sample(self):
        self.web.start()
        self.web._on_request()
        served = self.web.server.snapshot()["hosts"][0]
        self.assertGreater(served["load_fraction"], 0)


if __name__ == "__main__":
    unittest.main()
