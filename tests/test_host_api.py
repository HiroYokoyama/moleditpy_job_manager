"""The API's host routes, force run and re-check -- the contract, with no Qt.

Runs on the CI job that installs only pytest, like test_api_core.
"""

import os
import shutil
import tempfile
import unittest

from job_manager import api_core, store
from job_manager.api_core import ApiError, Deferred, JobApi
from job_manager.models import (
    MODE_RUNNER,
    STATE_DONE,
    STATE_LOST,
    STATE_PENDING,
    STATE_RUNNING,
    HostProfile,
    Job,
)

from .api_support import FakeService


class HostApiCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="jm_host_api_")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.store = store.JobStore(self.directory)
        self.cluster = self.store.add_host(
            HostProfile(name="mycluster", hostname="myhost", scheduler="slurm")
        )
        self.box = self.store.add_host(
            HostProfile(
                name="my box", hostname="mybox", scheduler="shell", concurrency_mode=MODE_RUNNER
            )
        )
        self.service = FakeService(self.store)
        self.service.answers = {}
        self.api = JobApi(self.service)
        handle, self.input_path = tempfile.mkstemp(suffix=".inp", dir=self.directory)
        os.close(handle)

    def call(self, method, path, query=None, body=None):
        status, payload = self.api.handle(method, api_core.API_PREFIX + path, query or {}, body)
        if isinstance(payload, Deferred):
            payload = payload.wait(1)
        return status, payload

    def add_job(self, host, **fields):
        job = Job(host_id=host.id, host_name=host.name, scheduler=host.scheduler, **fields)
        self.store.add_job(job)
        return job


class TestHostStatus(HostApiCase):
    def test_a_helper_queue_is_listed_in_order_with_what_is_ahead(self):
        waiting_a = self.add_job(self.box, name="first", state=STATE_PENDING)
        waiting_b = self.add_job(self.box, name="second", state=STATE_PENDING)
        running = self.add_job(self.box, name="busy", state=STATE_RUNNING)
        self.service.answers["status"] = {
            "stats": {"cores": 8, "threads": 16, "mem_free_mb": 1000},
            "queue": {
                "paused": False,
                "limits": {"slots": 9999, "cores": 8},
                "running": [{"entry": "e", "job_id": running.id, "cores": 6, "memory_mb": 0}],
                "waiting": [
                    {"entry": "a", "job_id": waiting_a.id, "cores": 1, "memory_mb": 0},
                    {"entry": "b", "job_id": waiting_b.id, "cores": 4, "memory_mb": 2048},
                ],
            },
        }

        _, reply = self.call("GET", f"/hosts/{self.box.id}/status")

        self.assertEqual(reply["stats"]["cores"], 8)
        queue = reply["queue"]
        self.assertEqual(queue["kind"], "helper")
        self.assertEqual(queue["cores_in_use"], 6)
        self.assertEqual([w["name"] for w in queue["waiting"]], ["first", "second"])
        self.assertEqual([w["ahead"] for w in queue["waiting"]], [0, 1])
        by_name = {job["name"]: job for job in reply["jobs"]}
        self.assertEqual(by_name["second"]["position"], 2)
        self.assertEqual(by_name["busy"]["queue"], "running")

    def test_a_host_is_found_by_a_name_with_a_space_in_it(self):
        self.call("GET", "/hosts/my%20box/status")
        self.assertEqual(self.service.status_calls[-1][0], self.box.id)

    def test_a_cluster_says_its_scheduler_keeps_the_queue(self):
        _, reply = self.call("GET", f"/hosts/{self.cluster.id}/status")
        self.assertEqual(reply["queue"], {"kind": "scheduler", "scheduler": "slurm"})

    def test_stats_can_be_left_out(self):
        self.call("GET", f"/hosts/{self.box.id}/status", {"stats": "0"})
        self.assertEqual(self.service.status_calls[-1], (self.box.id, False))

    def test_a_host_not_to_be_sampled_is_not(self):
        # A shared login node: its profile says its load is not ours to read.
        self.box.monitor_usage = False
        _, reply = self.call("GET", f"/hosts/{self.box.id}/status")
        self.assertEqual(self.service.status_calls[-1], (self.box.id, False))
        self.assertIn("switched off", reply["stats_skipped"])

    def test_a_disabled_host_is_refused(self):
        self.box.enabled = False
        with self.assertRaises(ApiError) as caught:
            self.call("GET", f"/hosts/{self.box.id}/status")
        self.assertEqual(caught.exception.status, 409)

    def test_a_host_that_does_not_answer_is_a_502(self):
        self.service.fail_with = "Connection refused"
        with self.assertRaises(ApiError) as caught:
            self.call("GET", f"/hosts/{self.box.id}/status")
        self.assertEqual(caught.exception.status, 502)


class TestHostFiles(HostApiCase):
    def test_listing_a_directory(self):
        self.service.answers["files"] = ["a.out", "sub/"]
        _, reply = self.call("GET", f"/hosts/{self.box.id}/files", {"path": "/opt/data"})
        self.assertEqual(reply["entries"], ["a.out", "sub/"])
        self.assertEqual(self.service.listed_paths, [("/opt/data", 1)])

    def test_depth_is_bounded(self):
        self.call("GET", f"/hosts/{self.box.id}/files", {"path": "/opt", "depth": "3"})
        self.assertEqual(self.service.listed_paths[-1], ("/opt", 3))
        with self.assertRaises(ApiError) as caught:
            self.call("GET", f"/hosts/{self.box.id}/files", {"path": "/opt", "depth": "9"})
        self.assertEqual(caught.exception.status, 400)

    def test_a_path_is_required_and_cannot_hold_a_line_break(self):
        for query in ({}, {"path": "  "}, {"path": "/opt/a\nrm -rf ~"}):
            with self.subTest(query=query), self.assertRaises(ApiError) as caught:
                self.call("GET", f"/hosts/{self.box.id}/file", query)
            self.assertEqual(caught.exception.status, 400)

    def test_stat_passes_the_hash_choice_through(self):
        self.service.answers["stat"] = {"exists": True, "type": "file", "size": 3}
        _, reply = self.call("GET", f"/hosts/{self.box.id}/file", {"path": "/opt/a", "hash": "0"})
        self.assertEqual(reply["size"], 3)
        self.assertEqual(self.service.stated, [("/opt/a", False)])

    def test_download_reports_what_landed_and_what_did_not(self):
        self.service.answers["download"] = (
            ["/local/a.out"],
            [("/opt/b.out", "no such file on the host")],
        )
        _, reply = self.call(
            "POST", f"/hosts/{self.box.id}/download", body={"paths": ["/opt/a.out", "/opt/b.out"]}
        )
        self.assertEqual(reply["files"], ["/local/a.out"])
        self.assertEqual(
            reply["skipped"], [{"path": "/opt/b.out", "reason": "no such file on the host"}]
        )
        self.assertEqual(reply["into"], "/downloads/auto")

    def test_download_into_a_folder_that_is_not_there_is_refused(self):
        with self.assertRaises(ApiError) as caught:
            self.call(
                "POST",
                f"/hosts/{self.box.id}/download",
                body={"paths": ["/opt/a"], "into": os.path.join(self.directory, "nope")},
            )
        self.assertEqual(caught.exception.status, 400)

    def test_download_waits_longer_than_a_listing(self):
        _, payload = self.api.handle(
            "POST",
            api_core.API_PREFIX + f"/hosts/{self.box.id}/download",
            {},
            {"paths": "/opt/a"},
        )
        self.assertEqual(payload.timeout, api_core.DOWNLOAD_TIMEOUT)


class TestForce(HostApiCase):
    def test_a_refused_force_is_a_409_with_the_reason(self):
        job = self.add_job(self.cluster, name="j", state=STATE_PENDING)
        self.service.refusal = "j is not in a helper queue."
        with self.assertRaises(ApiError) as caught:
            self.call("POST", f"/jobs/{job.id}/force", body={})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("helper queue", caught.exception.message)

    def test_a_force_answers_with_the_job(self):
        job = self.add_job(self.box, name="j", state=STATE_PENDING)
        _, reply = self.call("POST", f"/jobs/{job.id}/force", body={})
        self.assertTrue(reply["forced"])
        self.assertEqual(reply["job"]["id"], job.id)

    def test_a_job_that_already_left_the_queue_is_a_409(self):
        job = self.add_job(self.box, name="j", state=STATE_PENDING)
        self.service.answers["force"] = False
        with self.assertRaises(ApiError) as caught:
            self.call("POST", f"/jobs/{job.id}/force", body={})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("no longer waiting", caught.exception.message)

    def test_force_run_on_submit_is_passed_through(self):
        self.call(
            "POST",
            "/jobs",
            body={
                "host": "my box",
                "files": [self.input_path],
                "command": "mycommand {input}",
                "force_run": True,
            },
        )
        self.assertTrue(self.service.submitted[-1][4]["force_run"])

    def test_force_run_is_refused_where_a_scheduler_keeps_the_order(self):
        with self.assertRaises(ApiError) as caught:
            self.call(
                "POST",
                "/jobs",
                body={
                    "host": "mycluster",
                    "files": [self.input_path],
                    "command": "mycommand",
                    "force_run": True,
                },
            )
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("slurm", caught.exception.message)

    def test_force_run_cannot_also_wait(self):
        before = self.add_job(self.box, name="before", state=STATE_RUNNING)
        for extra in ({"after_job": before.id}, {"start_after": 1900000000}):
            with self.subTest(extra=extra), self.assertRaises(ApiError) as caught:
                body = {
                    "host": "my box",
                    "files": [self.input_path],
                    "command": "mycommand",
                    "force_run": True,
                }
                body.update(extra)
                self.call("POST", "/jobs", body=body)
            self.assertEqual(caught.exception.status, 400)


class TestRecheck(HostApiCase):
    def test_only_a_lost_job_is_rechecked(self):
        job = self.add_job(self.box, name="j", state=STATE_DONE)
        with self.assertRaises(ApiError) as caught:
            self.call("POST", f"/jobs/{job.id}/recheck", body={})
        self.assertEqual(caught.exception.status, 409)

    def test_the_reply_carries_the_evidence_and_the_job(self):
        job = self.add_job(self.box, name="j", state=STATE_LOST)
        self.service.answers["recheck"] = {
            "previous_state": STATE_LOST,
            "state": STATE_DONE,
            "changed": True,
            "rc": 0,
            "sentinel": "0",
            "runner_status": "0",
            "files": ["mol.out"],
        }
        _, reply = self.call("POST", f"/jobs/{job.id}/recheck", body={})
        self.assertEqual(reply["state"], STATE_DONE)
        self.assertTrue(reply["changed"])
        self.assertEqual(reply["files"], ["mol.out"])
        self.assertEqual(reply["job"]["id"], job.id)


if __name__ == "__main__":
    unittest.main()
