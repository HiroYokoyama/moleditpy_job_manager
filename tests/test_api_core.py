"""The API contract, with no socket and no Qt.

Everything a client can get wrong is answered with a status and a sentence, and
both are asserted here: a 500 with a traceback is not an API, and neither is a
200 that quietly ran the wrong command. The CI job that installs only pytest
runs this file, so nothing in it may import PyQt6.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest

from job_manager import api_core, store
from job_manager.api_core import ApiError, Deferred, JobApi
from .api_support import FakeService
from job_manager.models import (
    STATE_DONE,
    STATE_RUNNING,
    HostProfile,
    Job,
    SubmitPreset,
)


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="jm_api_")
        self.store = store.JobStore(self.directory)
        self.host = self.store.add_host(
            HostProfile(name="mycluster", hostname="myhost", scheduler="slurm")
        )
        self.service = FakeService(self.store)
        self.api = JobApi(self.service)
        handle, self.input_path = tempfile.mkstemp(suffix=".inp", dir=self.directory)
        os.close(handle)

    def get(self, path, **query):
        return self.api.handle("GET", api_core.API_PREFIX + path, query, {})

    def post(self, path, **body):
        return self.api.handle("POST", api_core.API_PREFIX + path, {}, body)

    def submit_body(self, **overrides):
        body = {
            "host": "mycluster",
            "files": [self.input_path],
            "command": "mycommand {input} > {stem}.out",
        }
        body.update(overrides)
        return body

    def add_job(self, **fields):
        job = Job(host_id=self.host.id, host_name=self.host.name, **fields)
        self.store.add_job(job)
        return job


class TestRouting(ApiTestCase):
    def test_a_path_outside_the_prefix_is_a_404_that_says_where_to_go(self):
        with self.assertRaises(ApiError) as caught:
            self.api.handle("GET", "/jobs", {}, {})
        self.assertEqual(caught.exception.status, 404)
        self.assertIn(api_core.API_PREFIX, caught.exception.message)

    def test_the_bare_prefix_answers_like_ping(self):
        status, payload = self.api.handle("GET", api_core.API_PREFIX, {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_ping_reports_the_plugin_version_that_is_installed(self):
        from job_manager import PLUGIN_VERSION

        _, payload = self.get("/ping")
        self.assertEqual(payload["plugin_version"], PLUGIN_VERSION)
        self.assertEqual(payload["api_version"], api_core.API_VERSION)

    def test_the_wrong_method_is_a_405_naming_the_right_one(self):
        with self.assertRaises(ApiError) as caught:
            self.api.handle("DELETE", api_core.API_PREFIX + "/hosts", {}, {})
        self.assertEqual(caught.exception.status, 405)
        self.assertIn("GET", caught.exception.message)

    def test_an_unknown_job_action_is_a_404(self):
        job = self.add_job(name="j")
        with self.assertRaises(ApiError) as caught:
            self.post(f"/jobs/{job.id}/explode")
        self.assertEqual(caught.exception.status, 404)


class TestReads(ApiTestCase):
    def test_hosts_omit_everything_a_client_has_no_business_with(self):
        self.host.ssh_options = ["ProxyCommand=something"]
        self.host.login_commands = ["module load private"]
        _, payload = self.get("/hosts")
        served = payload["hosts"][0]
        self.assertEqual(served["name"], "mycluster")
        self.assertNotIn("ssh_options", served)
        self.assertNotIn("login_commands", served)
        self.assertNotIn("key_path", served)

    def test_presets_can_be_narrowed_to_one_host(self):
        other = self.store.add_host(HostProfile(name="other"))
        self.store.add_preset(SubmitPreset(host_id=self.host.id, name="big"))
        self.store.add_preset(SubmitPreset(host_id=other.id, name="elsewhere"))
        _, payload = self.get("/presets", host="mycluster")
        self.assertEqual([p["name"] for p in payload["presets"]], ["big"])

    def test_a_preset_reads_back_under_the_name_a_submission_writes(self):
        self.store.add_preset(
            SubmitPreset(host_id=self.host.id, name="big", command_template="prog {input}")
        )
        _, payload = self.get("/presets")
        self.assertEqual(payload["presets"][0]["command"], "prog {input}")

    def test_jobs_can_be_filtered_by_state_by_host_and_by_name(self):
        self.add_job(name="running one", state=STATE_RUNNING)
        self.add_job(name="finished one", state=STATE_DONE)
        self.assertEqual(len(self.get("/jobs")[1]["jobs"]), 2)
        self.assertEqual(len(self.get("/jobs", state="ACTIVE")[1]["jobs"]), 1)
        self.assertEqual(len(self.get("/jobs", state="TERMINAL")[1]["jobs"]), 1)
        self.assertEqual(len(self.get("/jobs", state="DONE")[1]["jobs"]), 1)
        self.assertEqual(len(self.get("/jobs", host="mycluster")[1]["jobs"]), 2)
        self.assertEqual(len(self.get("/jobs", name="finished")[1]["jobs"]), 1)
        self.assertEqual(len(self.get("/jobs", limit=1)[1]["jobs"]), 1)

    def test_a_job_carries_the_derived_fields_a_client_would_have_to_compute(self):
        job = self.add_job(name="j", state=STATE_RUNNING)
        _, payload = self.get(f"/jobs/{job.id}")
        self.assertTrue(payload["job"]["active"])
        self.assertFalse(payload["job"]["terminal"])
        self.assertIn("elapsed_seconds", payload["job"])
        self.assertEqual(payload["job"]["blocked_by"], "")

    def test_an_unknown_job_is_a_404(self):
        with self.assertRaises(ApiError) as caught:
            self.get("/jobs/nosuchjob")
        self.assertEqual(caught.exception.status, 404)


class TestSubmission(ApiTestCase):
    def test_a_submission_reaches_the_service_and_returns_the_job(self):
        status, payload = self.post("/jobs", **self.submit_body(name="water"))
        self.assertEqual(status, 202)
        self.assertEqual(payload["job"]["name"], "water")
        host, preset, name, files, _ = self.service.submitted[0]
        self.assertEqual(host.id, self.host.id)
        self.assertEqual(preset.command_template, "mycommand {input} > {stem}.out")
        self.assertEqual(files, [self.input_path])

    def test_a_host_can_be_named_rather_than_identified(self):
        self.post("/jobs", **self.submit_body(host=self.host.id))
        self.post("/jobs", **self.submit_body(host="MYCLUSTER"))
        self.assertEqual({call[0].id for call in self.service.submitted}, {self.host.id})

    def test_an_unknown_host_is_a_404_that_lists_the_known_ones(self):
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(host="nowhere"))
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("mycluster", caught.exception.message)

    def test_a_disabled_host_is_refused_rather_than_submitted_to(self):
        self.host.enabled = False
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body())
        self.assertEqual(caught.exception.status, 409)

    def test_a_missing_input_file_is_a_400_naming_it(self):
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(files=["/no/such/file.inp"]))
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("file.inp", caught.exception.message)

    def test_a_submission_with_no_command_and_no_preset_is_refused(self):
        # Not defaulted: SubmitPreset's own template names one program, and a
        # caller that forgot the command would have run it on their input.
        body = self.submit_body()
        body.pop("command")
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **body)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("command", caught.exception.message)
        self.assertEqual(self.service.submitted, [])

    def test_an_empty_command_is_refused_too(self):
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(command="   "))
        self.assertEqual(caught.exception.status, 400)

    def test_nothing_to_run_is_refused(self):
        body = self.submit_body(files=[])
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **body)
        self.assertEqual(caught.exception.status, 400)

    def test_work_already_on_the_host_needs_no_input_file(self):
        self.post("/jobs", **self.submit_body(files=[], remote_dir="/scratch/run", name="staged"))
        self.assertEqual(self.service.submitted[0][4]["remote_dir"], "/scratch/run")

    def test_a_remote_input_without_a_remote_dir_is_refused(self):
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(remote_input="a.inp"))
        self.assertEqual(caught.exception.status, 400)

    def test_a_named_preset_supplies_the_resource_request(self):
        self.store.add_preset(
            SubmitPreset(
                host_id=self.host.id,
                name="big",
                command_template="prog {input}",
                walltime="48:00:00",
                cpus_per_task=32,
            )
        )
        body = self.submit_body()
        body.pop("command")
        self.post("/jobs", **body, preset="big")
        preset = self.service.submitted[0][1]
        self.assertEqual(preset.walltime, "48:00:00")
        self.assertEqual(preset.cpus_per_task, 32)
        self.assertEqual(preset.command_template, "prog {input}")

    def test_explicit_fields_override_the_named_preset(self):
        self.store.add_preset(
            SubmitPreset(
                host_id=self.host.id, name="big", command_template="prog", walltime="1:00:00"
            )
        )
        self.post("/jobs", **self.submit_body(preset="big", walltime="9:00:00", cpus_per_task=8))
        preset = self.service.submitted[0][1]
        self.assertEqual(preset.walltime, "9:00:00")
        self.assertEqual(preset.cpus_per_task, 8)

    def test_overriding_does_not_edit_the_stored_preset(self):
        stored = self.store.add_preset(
            SubmitPreset(
                host_id=self.host.id, name="big", command_template="prog", walltime="1:00:00"
            )
        )
        self.post("/jobs", **self.submit_body(preset="big", walltime="9:00:00"))
        self.assertEqual(stored.walltime, "1:00:00")
        self.assertNotEqual(self.service.submitted[0][1].id, stored.id)

    def test_a_preset_on_another_host_is_not_found(self):
        other = self.store.add_host(HostProfile(name="other"))
        self.store.add_preset(SubmitPreset(host_id=other.id, name="big", command_template="prog"))
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(preset="big"))
        self.assertEqual(caught.exception.status, 404)

    def test_a_number_field_rejects_a_string_and_a_boolean(self):
        for value in ("eight", True):
            with self.assertRaises(ApiError) as caught:
                self.post("/jobs", **self.submit_body(cpus_per_task=value))
            self.assertEqual(caught.exception.status, 400)

    def test_a_list_field_rejects_a_bare_string(self):
        # "*.out" is a plausible mistake, and iterating it would have fetched
        # one glob per character.
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(fetch_globs="*.out"))
        self.assertEqual(caught.exception.status, 400)

    def test_auto_download_can_be_switched_off_per_submission(self):
        self.post("/jobs", **self.submit_body(auto_download=False))
        self.assertFalse(self.service.submitted[0][4]["auto_download"])

    def test_chaining_takes_a_job_id_on_the_same_host(self):
        first = self.add_job(name="first", state=STATE_RUNNING)
        self.post("/jobs", **self.submit_body(after_job=first.id))
        self.assertIs(self.service.submitted[0][4]["after_job"], first)

    def test_chaining_across_hosts_is_refused(self):
        other = self.store.add_host(HostProfile(name="other"))
        elsewhere = Job(name="elsewhere", host_id=other.id, state=STATE_RUNNING)
        self.store.add_job(elsewhere)
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(after_job=elsewhere.id))
        self.assertEqual(caught.exception.status, 400)

    def test_start_after_accepts_an_epoch_second_and_a_local_time(self):
        self.post("/jobs", **self.submit_body(start_after=1786000000))
        self.assertEqual(self.service.submitted[0][4]["start_after"], 1786000000.0)
        self.post("/jobs", **self.submit_body(start_after="2026-01-31T18:30"))
        self.assertGreater(self.service.submitted[1][4]["start_after"], 0)

    def test_an_unparseable_start_after_is_a_400(self):
        with self.assertRaises(ApiError) as caught:
            self.post("/jobs", **self.submit_body(start_after="next tuesday"))
        self.assertEqual(caught.exception.status, 400)

    def test_a_relative_input_path_is_resolved_before_it_is_used(self):
        # The server's working directory is MoleditPy's, not the caller's, so
        # a path that is relative when it arrives is already a bug.
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.directory)
        name = os.path.basename(self.input_path)
        self.post("/jobs", **self.submit_body(files=[name]))
        self.assertTrue(os.path.isabs(self.service.submitted[0][3][0]))


class TestJobActions(ApiTestCase):
    def test_cancel_reaches_the_service(self):
        job = self.add_job(name="j", state=STATE_RUNNING)
        _, payload = self.post(f"/jobs/{job.id}/cancel")
        self.assertTrue(payload["cancelling"])
        self.assertEqual(self.service.cancelled, [(job.id, True)])

    def test_cancelling_a_finished_job_is_a_409(self):
        job = self.add_job(name="j", state=STATE_DONE)
        with self.assertRaises(ApiError) as caught:
            self.post(f"/jobs/{job.id}/cancel")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.cancelled, [])

    def test_download_starts_one_and_says_so(self):
        job = self.add_job(name="j", state=STATE_DONE)
        _, payload = self.post(f"/jobs/{job.id}/download")
        self.assertTrue(payload["downloading"])
        self.assertEqual(self.service.downloads, [(job.id, "", None)])

    def test_a_download_already_running_is_a_409_not_a_second_one(self):
        job = self.add_job(name="j", state=STATE_DONE)
        self.service.download_returns = False
        with self.assertRaises(ApiError) as caught:
            self.post(f"/jobs/{job.id}/download")
        self.assertEqual(caught.exception.status, 409)

    def test_download_into_a_path_that_is_not_a_directory_is_a_400(self):
        job = self.add_job(name="j", state=STATE_DONE)
        with self.assertRaises(ApiError) as caught:
            self.post(f"/jobs/{job.id}/download", into=self.input_path)
        self.assertEqual(caught.exception.status, 400)

    def test_waiting_for_a_download_answers_when_the_files_are_here(self):
        job = self.add_job(name="j", state=STATE_DONE)
        _, deferred = self.post(f"/jobs/{job.id}/download", wait=True)
        self.assertIsInstance(deferred, Deferred)
        self.service.results_ready.emit(job.id, ["/tmp/out.log"])
        self.assertEqual(deferred.wait(1)["files"], ["/tmp/out.log"])

    def test_another_jobs_download_does_not_answer_this_request(self):
        job = self.add_job(name="j", state=STATE_DONE)
        other = self.add_job(name="other", state=STATE_DONE)
        _, deferred = self.post(f"/jobs/{job.id}/download", wait=True)
        self.service.results_ready.emit(other.id, ["/tmp/wrong.log"])
        with self.assertRaises(ApiError) as caught:
            deferred.wait(0.05)
        self.assertEqual(caught.exception.status, 504)

    def test_a_failed_download_becomes_an_error_not_a_hang(self):
        job = self.add_job(name="j", state=STATE_DONE)
        _, deferred = self.post(f"/jobs/{job.id}/download", wait=True)
        self.service.error.emit("the host refused")
        with self.assertRaises(ApiError) as caught:
            deferred.wait(1)
        self.assertIn("refused", caught.exception.message)

    def test_a_finished_wait_leaves_no_handler_connected(self):
        # Left connected, every later download would answer a request that
        # was served long ago -- and keep this Deferred alive for the session.
        job = self.add_job(name="j", state=STATE_DONE)
        _, deferred = self.post(f"/jobs/{job.id}/download", wait=True)
        self.service.results_ready.emit(job.id, [])
        self.assertEqual(self.service.results_ready.slots, [])
        self.assertEqual(self.service.error.slots, [])

    def test_the_log_tail_is_deferred_until_the_host_answers(self):
        job = self.add_job(name="j", state=STATE_RUNNING, log_file="job.log")
        _, deferred = self.get(f"/jobs/{job.id}/log", lines=50)
        self.assertEqual(self.service.tails, [(job.id, "job.log", 50)])
        self.service._tail_done("...output...")
        self.assertEqual(deferred.wait(1)["text"], "...output...")

    def test_another_file_in_the_job_directory_can_be_tailed(self):
        job = self.add_job(name="j", state=STATE_RUNNING, log_file="job.log")
        self.get(f"/jobs/{job.id}/log", file="mol.out")
        self.assertEqual(self.service.tails[0][1], "mol.out")

    def test_listing_a_job_with_no_remote_directory_is_a_409(self):
        job = self.add_job(name="j", state=STATE_RUNNING)
        with self.assertRaises(ApiError) as caught:
            self.get(f"/jobs/{job.id}/files")
        self.assertEqual(caught.exception.status, 409)

    def test_listing_answers_with_the_remote_names(self):
        job = self.add_job(name="j", state=STATE_DONE, remote_dir="/scratch/j")
        _, deferred = self.get(f"/jobs/{job.id}/files")
        self.service._list_ok(["mol.out", "mol.xyz"])
        self.assertEqual(deferred.wait(1)["files"], ["mol.out", "mol.xyz"])

    def test_forgetting_an_active_job_is_refused(self):
        job = self.add_job(name="j", state=STATE_RUNNING)
        with self.assertRaises(ApiError) as caught:
            self.api.handle("DELETE", f"{api_core.API_PREFIX}/jobs/{job.id}", {}, {})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.removed, [])

    def test_forgetting_a_finished_job_removes_it(self):
        job = self.add_job(name="j", state=STATE_DONE)
        status, payload = self.api.handle("DELETE", f"{api_core.API_PREFIX}/jobs/{job.id}", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"], job.id)
        self.assertNotIn(job.id, self.store.jobs)


class TestTheToken(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="jm_api_token_")

    def test_a_token_is_generated_once_and_then_reused(self):
        first = api_core.ensure_token(self.directory)
        self.assertTrue(first)
        self.assertEqual(api_core.ensure_token(self.directory), first)
        self.assertEqual(api_core.read_token(self.directory), first)

    def test_renewing_replaces_it(self):
        first = api_core.ensure_token(self.directory)
        second = api_core.ensure_token(self.directory, renew=True)
        self.assertNotEqual(first, second)
        self.assertEqual(api_core.read_token(self.directory), second)

    def test_a_token_is_long_enough_to_be_a_secret(self):
        # 32 random bytes, urlsafe-encoded. Guessing is not the attack this
        # defends against, but a short token would make it one.
        self.assertGreaterEqual(len(api_core.ensure_token(self.directory)), 40)

    @unittest.skipIf(os.name == "nt", "POSIX permissions; Windows uses the directory ACL")
    def test_the_token_file_is_readable_only_by_this_user(self):
        api_core.ensure_token(self.directory)
        mode = os.stat(api_core.token_path(self.directory)).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_no_token_file_reads_as_no_token_rather_than_raising(self):
        self.assertEqual(api_core.read_token(self.directory), "")

    def test_comparing_tokens_rejects_the_empty_string_on_both_sides(self):
        # Without this, an unstarted server (no token) would have authenticated
        # a client that sent no token either.
        self.assertFalse(api_core.tokens_match("", ""))
        self.assertFalse(api_core.tokens_match("abc", ""))
        self.assertFalse(api_core.tokens_match("", "abc"))
        self.assertTrue(api_core.tokens_match("abc", "abc"))

    def test_the_endpoint_file_names_the_port_and_the_token(self):
        token = api_core.ensure_token(self.directory)
        path = api_core.write_endpoint_file(self.directory, 8765, token)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["port"], 8765)
        self.assertEqual(data["token"], token)
        self.assertTrue(data["url"].startswith("http://127.0.0.1:8765"))
        self.assertIn(api_core.API_PREFIX, data["url"])

    def test_removing_the_endpoint_file_twice_is_not_an_error(self):
        api_core.write_endpoint_file(self.directory, 1, "t")
        api_core.remove_endpoint_file(self.directory)
        api_core.remove_endpoint_file(self.directory)
        self.assertFalse(os.path.exists(api_core.endpoint_path(self.directory)))


class TestThePreferences(unittest.TestCase):
    def test_the_api_is_off_by_default(self):
        # The whole security posture rests on this: nothing listens until the
        # user says so, on a machine where the plugin was merely installed.
        self.assertFalse(store.DEFAULT_PREFS["api_enabled"])

    def test_the_default_port_matches_the_one_the_server_uses(self):
        self.assertEqual(store.DEFAULT_PREFS["api_port"], api_core.DEFAULT_PORT)


if __name__ == "__main__":
    unittest.main()


class TestTheTokenIsSafeOnACommandLine(unittest.TestCase):
    """The shipped CLI takes --token, so the secret has to survive argparse.

    token_urlsafe draws from the base64url alphabet, and roughly one token in
    sixty-four begins with "-". argparse reads that as an option name and
    refuses the whole command with "argument --token: expected one argument",
    which names neither the token nor the fix.
    """

    def test_a_generated_token_never_starts_with_a_hyphen(self):
        # Sixty-four thousand draws: at the 1-in-64 natural rate this would
        # see roughly a thousand of them if the guard were removed.
        self.assertFalse(any(api_core.new_token(32).startswith("-") for _ in range(64000)))

    def test_it_is_still_a_secret_of_the_expected_size(self):
        # The guard must reroll, not truncate or rewrite the first character.
        token = api_core.new_token(32)
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(len(set(api_core.new_token(32) for _ in range(500))), 500)

    def test_the_stored_token_goes_through_the_same_guard(self):
        directory = tempfile.mkdtemp(prefix="jm_token_")
        self.addCleanup(shutil.rmtree, directory, True)
        self.assertFalse(api_core.ensure_token(directory).startswith("-"))

    def test_argparse_accepts_a_generated_token_as_a_separate_argument(self):
        # The shape the CLI is actually invoked with. Asserting the property
        # through argparse itself, rather than only through a string check,
        # is what ties the guard to the reason it exists.
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--token")
        parser.add_argument("cmd")
        for _ in range(2000):
            parsed = parser.parse_args(["--token", api_core.new_token(32), "ping"])
            self.assertTrue(parsed.token)
