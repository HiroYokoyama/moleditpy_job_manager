"""The client, against a server that is really listening.

This is the file that answers "can another program actually drive this?", so
it uses the shipped client rather than hand-built requests, and it goes through
discovery rather than being handed a URL -- both are what a caller does.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import pytest

pytest.importorskip("PyQt6.QtCore", reason="PyQt6 is not installed")

from job_manager import api_core  # noqa: E402
from job_manager.api_client import JobApiError, JobManagerClient, build_parser, main  # noqa: E402
from job_manager.api_client import TERMINAL_STATES  # noqa: E402
from job_manager.api_server import JobApiServer  # noqa: E402
from job_manager.models import STATE_DONE, STATE_RUNNING, HostProfile, Job  # noqa: E402
from job_manager.store import JobStore  # noqa: E402

from .api_support import FakeService, in_thread, when_ready  # noqa: E402


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="jm_api_client_")
        self.store = JobStore(self.directory)
        self.host = self.store.add_host(HostProfile(name="mycluster", scheduler="slurm"))
        self.service = FakeService(self.store)
        self.server = JobApiServer(self.service)
        self.server.start(0)
        self.addCleanup(self.server.stop)
        self.client = JobManagerClient(directory=self.directory)
        handle, self.input_path = tempfile.mkstemp(suffix=".inp", dir=self.directory)
        os.close(handle)

    def add_job(self, **fields):
        job = Job(host_id=self.host.id, host_name=self.host.name, **fields)
        self.store.add_job(job)
        return job

    def later(self, delay, fn):
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(delay, fn)


class TestDiscovery(ClientTestCase):
    def test_a_client_finds_the_running_server_with_no_configuration(self):
        found = api_core.endpoint_path(self.directory)
        self.assertTrue(os.path.exists(found))
        self.assertEqual(self.client.url, self.server.url())
        self.assertEqual(self.client.token, self.server.token())

    def test_a_stopped_server_is_reported_as_not_running_not_as_a_crash(self):
        self.server.stop()
        with self.assertRaises(JobApiError) as caught:
            JobManagerClient(directory=self.directory)
        self.assertIn("not running", str(caught.exception))

    def test_a_url_and_token_in_the_environment_skip_discovery(self):
        from unittest.mock import patch

        with patch.dict(
            os.environ,
            {"MOLEDITPY_JOB_API_URL": self.server.url(), "MOLEDITPY_JOB_API_TOKEN": "t"},
        ):
            # A directory with no endpoint file: discovery would have raised.
            client = JobManagerClient(directory=tempfile.mkdtemp(prefix="jm_empty_"))
        self.assertEqual(client.token, "t")

    def test_a_damaged_endpoint_file_is_reported_rather_than_swallowed(self):
        with open(api_core.endpoint_path(self.directory), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        with self.assertRaises(JobApiError):
            JobManagerClient(directory=self.directory)


class TestTheClientRoutes(ClientTestCase):
    def test_ping(self):
        reply = in_thread(self.client.ping)
        self.assertTrue(reply["ok"])

    def test_hosts_and_presets(self):
        from job_manager.models import SubmitPreset

        self.store.add_preset(
            SubmitPreset(host_id=self.host.id, name="big", command_template="prog {input}")
        )
        hosts = in_thread(self.client.hosts)
        self.assertEqual([h["name"] for h in hosts], ["mycluster"])
        presets = in_thread(lambda: self.client.presets("mycluster"))
        self.assertEqual(presets[0]["command"], "prog {input}")

    def test_submit_returns_a_job_that_is_being_tracked(self):
        job = in_thread(
            lambda: self.client.submit(
                host="mycluster",
                files=[self.input_path],
                command="mycommand {input}",
                name="water",
            )
        )
        self.assertEqual(job["name"], "water")
        self.assertIn(job["id"], self.store.jobs)

    def test_submit_resolves_a_relative_path_on_the_callers_side(self):
        # The server would resolve it against MoleditPy's directory, which is
        # not where the caller is standing.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.directory)
        name = os.path.basename(self.input_path)
        in_thread(
            lambda: self.client.submit(host="mycluster", files=name, command="mycommand {input}")
        )
        self.assertEqual(self.service.submitted[0][3], [self.input_path])

    def test_a_refused_submission_raises_with_the_status_and_the_reason(self):
        with self.assertRaises(JobApiError) as caught:
            in_thread(lambda: self.client.submit(host="nowhere", command="x"))
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("mycluster", str(caught.exception))

    def test_jobs_and_job(self):
        job = self.add_job(name="one", state=STATE_RUNNING)
        listed = in_thread(lambda: self.client.jobs(state="ACTIVE"))
        self.assertEqual([j["id"] for j in listed], [job.id])
        self.assertEqual(in_thread(lambda: self.client.job(job.id))["name"], "one")

    def test_cancel(self):
        job = self.add_job(name="one", state=STATE_RUNNING)
        in_thread(lambda: self.client.cancel(job.id))
        self.assertEqual(self.service.cancelled, [(job.id, True)])

    def test_download_without_waiting(self):
        job = self.add_job(name="one", state=STATE_DONE)
        reply = in_thread(lambda: self.client.download(job.id))
        self.assertTrue(reply["downloading"])

    def test_download_waiting_returns_the_paths(self):
        job = self.add_job(name="one", state=STATE_DONE)
        when_ready(
            lambda: self.service.results_ready.slots,
            lambda: self.service.results_ready.emit(job.id, ["/tmp/one.out"]),
        )
        reply = in_thread(lambda: self.client.download(job.id, wait=True))
        self.assertEqual(reply["files"], ["/tmp/one.out"])

    def test_log_and_files(self):
        job = self.add_job(name="one", state=STATE_RUNNING, remote_dir="/scratch/one")
        when_ready(
            lambda: self.service._tail_done,
            lambda: self.service._tail_done("...tail..."),
        )
        self.assertEqual(in_thread(lambda: self.client.log(job.id, lines=5)), "...tail...")
        when_ready(
            lambda: self.service._list_ok,
            lambda: self.service._list_ok(["one.out"]),
        )
        self.assertEqual(in_thread(lambda: self.client.files(job.id)), ["one.out"])

    def test_forget(self):
        job = self.add_job(name="one", state=STATE_DONE)
        in_thread(lambda: self.client.forget(job.id))
        self.assertNotIn(job.id, self.store.jobs)


class TestWaiting(ClientTestCase):
    def test_wait_returns_once_the_job_reaches_a_terminal_state(self):
        job = self.add_job(name="one", state=STATE_RUNNING)
        self.later(60, lambda: job.touch(STATE_DONE))
        final = in_thread(lambda: self.client.wait(job.id, interval=0.5))
        self.assertEqual(final["state"], STATE_DONE)

    def test_wait_gives_up_rather_than_blocking_for_ever(self):
        job = self.add_job(name="one", state=STATE_RUNNING)
        with self.assertRaises(JobApiError) as caught:
            in_thread(lambda: self.client.wait(job.id, interval=0.5, timeout=0.6))
        self.assertIn(STATE_RUNNING, str(caught.exception))

    def test_the_clients_terminal_states_are_the_plugins(self):
        # The client is meant to be copyable and so repeats the list rather
        # than importing it; a state added on one side must reach the other.
        from job_manager.models import TERMINAL_STATES as REAL

        self.assertEqual(TERMINAL_STATES, REAL)


class TestTheCommandLine(ClientTestCase):
    def run_cli(self, *argv):
        buffer = io.StringIO()

        def go():
            with redirect_stdout(buffer):
                # "--token=x", not "--token", "x": token_urlsafe draws from the
                # base64url alphabet, so about one token in sixty-four begins
                # with "-" and argparse then reads it as an option name --
                # "argument --token: expected one argument". Passed separately,
                # every CLI test here was a 1.5% coin flip, which is why a
                # different one of them failed every few CI runs.
                code = main([f"--url={self.server.url()}", f"--token={self.server.token()}", *argv])
            return code, buffer.getvalue()

        return in_thread(go)

    def test_ping_prints_json(self):
        code, out = self.run_cli("ping")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_submit_from_the_command_line(self):
        code, out = self.run_cli(
            "submit",
            self.input_path,
            "--host",
            "mycluster",
            "--command",
            "mycommand {input}",
            "--cpus",
            "8",
            "--memory",
            "16GB",
        )
        self.assertEqual(code, 0)
        preset = self.service.submitted[0][1]
        self.assertEqual(preset.cpus_per_task, 8)
        self.assertEqual(preset.memory, "16GB")
        self.assertIn(self.store.job_list()[0].id, out)

    def test_a_numeric_start_after_is_sent_as_a_time_not_as_text(self):
        # "1786000000" is an epoch second; sent as a string the server would
        # have tried to read it as a date and refused the submission.
        self.run_cli(
            "submit",
            self.input_path,
            "--host",
            "mycluster",
            "--command",
            "mycommand {input}",
            "--start-after",
            "1786000000",
        )
        self.assertEqual(self.service.submitted[0][4]["start_after"], 1786000000.0)

    def test_jobs_prints_one_line_per_job(self):
        self.add_job(name="one", state=STATE_RUNNING)
        self.add_job(name="two", state=STATE_DONE)
        code, out = self.run_cli("jobs")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_a_failed_job_makes_wait_exit_non_zero(self):
        # So a shell script can chain on it without parsing the reply.
        job = self.add_job(name="one", state=STATE_RUNNING)
        self.later(60, lambda: job.touch("FAILED"))
        code, _ = self.run_cli("wait", job.id, "--interval", "0.5")
        self.assertEqual(code, 2)

    def test_an_api_error_is_a_message_and_exit_one_not_a_traceback(self):
        code, _ = self.run_cli("job", "nosuchjob")
        self.assertEqual(code, 1)

    def test_every_subcommand_is_reachable(self):
        parser = build_parser()
        for command in (
            "ping",
            "hosts",
            "presets",
            "jobs",
            "job",
            "submit",
            "cancel",
            "download",
            "log",
            "files",
            "wait",
            "forget",
        ):
            with self.subTest(command=command):
                self.assertIn(command, parser.format_help())


if __name__ == "__main__":
    unittest.main()
