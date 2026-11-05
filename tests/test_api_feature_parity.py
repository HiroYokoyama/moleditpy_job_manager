"""Submission options and host queue controls exposed to HTTP callers."""

from unittest.mock import Mock

import pytest

from job_manager.api_client import JobManagerClient, _run, _submit_fields, build_parser
from job_manager.api_types import ApiError
from job_manager.models import MODE_RUNNER
from .test_api_core import ApiTestCase


class TestFeatureParity(ApiTestCase):
    def test_ping_advertises_after_and_relay(self):
        _, data = self.get("/ping")
        assert {
            "after_job",
            "chain_any",
            "start_after",
            "relay_source_dir",
            "relay_filenames",
        } <= set(data["submit_fields"])

    def test_after_and_relay_reach_the_submission_workflow(self):
        parent = self.add_job(state="RUNNING", remote_dir="/scratch/previous")
        status, _ = self.post(
            "/jobs",
            **self.submit_body(
                after_job=parent.id,
                chain_any=True,
                relay_source_dir=parent.remote_dir,
                relay_filenames=["result.xyz", "nested/result.chk"],
            ),
        )
        assert status == 202
        options = self.service.submitted[-1][-1]
        assert options["after_job"] is parent and options["chain_any"]
        assert options["relay_filenames"] == ["result.xyz", "nested/result.chk"]
        assert options["relay_source_dir"] == parent.remote_dir

    def test_invalid_relay_is_refused_before_creating_a_job(self):
        for fields in (
            {"relay_source_dir": "/scratch/previous"},
            {"relay_filenames": ["result.xyz"]},
            *(
                {"relay_source_dir": "/scratch/previous", "relay_filenames": [name]}
                for name in ("../outside", "/absolute", "a/../b", "C:outside", "a\\b", "a\nfile", 7)
            ),
        ):
            with pytest.raises(ApiError) as error:
                self.post("/jobs", **self.submit_body(**fields))
            assert error.value.status == 400
        assert not self.service.submitted

    def test_host_queue_requires_helper_and_validates_types(self):
        path = f"/hosts/{self.host.id}/queue"
        with pytest.raises(ApiError) as error:
            self.post(path, paused=True)
        assert error.value.status == 409
        self.host.scheduler = "shell"
        self.host.concurrency_mode = MODE_RUNNER
        for body in (
            {},
            {"paused": "yes"},
            {"runner_cores": True},
            {"runner_memory_mb": -1},
            {"max_concurrent": 1.5},
            {"unknown": 1},
            {"paused": True, "runner_cores": 2},
        ):
            with pytest.raises(ApiError) as error:
                self.post(path, **body)
            assert error.value.status == 400
        for body in (
            {"paused": False},
            {"max_concurrent": 0, "runner_cores": 4, "runner_memory_mb": 2048},
        ):
            _, reply = self.post(path, **body)
            assert reply.wait(0) == body
            assert self.service.queue_changes == (self.host.id, body)
        self.service.fail_with = "connection failed"
        _, reply = self.post(path, paused=True)
        with pytest.raises(ApiError) as error:
            reply.wait(0)
        assert error.value.status == 502
        self.service.queue_change_in_flight = lambda host_id: True
        with pytest.raises(ApiError) as error:
            self.post(path, paused=True)
        assert error.value.status == 409
        with pytest.raises(ApiError) as error:
            self.get(path)
        assert error.value.status == 405


def test_client_queue_and_cli_submission_options():
    client = JobManagerClient(url="http://127.0.0.1:1", token="test")
    client.request = Mock(return_value={"paused": True})
    client.configure_queue("my box", paused=True)
    client.request.assert_called_once_with("POST", "hosts/my%20box/queue", body={"paused": True})
    args = build_parser().parse_args(
        [
            "submit",
            "--host",
            "myhost",
            "--after",
            "parent",
            "--relay-source-dir",
            "/scratch/previous",
            "--relay-file",
            "result.xyz",
            "--extra-directive",
            "#SBATCH --exclusive",
        ]
    )
    fields = _submit_fields(args)
    assert fields["after_job"] == "parent"
    assert fields["relay_filenames"] == ["result.xyz"]
    assert fields["extra_directives"] == ["#SBATCH --exclusive"]
    client.configure_queue = Mock()
    args = build_parser().parse_args(["host-queue", "myhost", "--resume"])
    _run(client, args)
    client.configure_queue.assert_called_once_with("myhost", paused=False)
