"""Forced submissions can wait for the previous forced job on a helper host."""

import pytest
from job_manager.api_client import _submit_fields, build_parser
from job_manager.api_types import ApiError
from job_manager.models import MODE_RUNNER, SCHEDULER_SHELL
from .test_api_core import ApiTestCase


class TestForcedWaitingApi(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.host.scheduler = SCHEDULER_SHELL
        self.host.concurrency_mode = MODE_RUNNER

    def test_forced_wait_is_forwarded_and_requires_helper_force(self):
        self.post("/jobs", **self.submit_body(force_run=True, force_after_previous=True))
        assert self.service.submitted[-1][4]["force_after_previous"]
        for fields in (
            {"force_after_previous": True},
            {"force_run": True, "force_after_previous": "yes"},
        ):
            with pytest.raises(ApiError) as error:
                self.post("/jobs", **self.submit_body(**fields))
            assert error.value.status == 400


def test_cli_forced_wait():
    args = build_parser().parse_args(
        ["submit", "--host", "myhost", "--force", "--force-after-previous"]
    )
    assert _submit_fields(args)["force_after_previous"] is True
