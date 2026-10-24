"""Reproductions from the full production-source audit; synthetic inputs only."""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from job_manager import host_stats, input_scan, remote_runner, remote_runner_ps
from job_manager.api_client import DOWNLOAD_TIMEOUT, JobManagerClient
from job_manager.api_inputs import ApiInputs
from job_manager.api_types import ApiError
from job_manager.models import HostProfile, Job, SubmitPreset, STATE_RUNNING
from job_manager.result_files import _download_atomic
from job_manager.runner import poll_runner, _start_runner
from job_manager.schedulers.base import requested_cores
from job_manager.store import JobStore
from job_manager.transport.base import CommandResult, TransportError
from .fakes import FakeTransport


def test_waiting_download_uses_transfer_timeout():
    client = object.__new__(JobManagerClient)
    client.request = Mock(return_value={})
    client.download("example", wait=True)
    assert client.request.call_args.kwargs["timeout"] == DOWNLOAD_TIMEOUT
    client.download("example", wait=True, timeout=17)
    assert client.request.call_args.kwargs["timeout"] == 17


def test_helper_reserves_all_task_cores():
    assert requested_cores(SubmitPreset(ntasks=8, cpus_per_task=2)) == 16


def test_failed_helper_query_does_not_report_lost():
    host = HostProfile()
    transport = FakeTransport(host)
    transport.default = CommandResult(255, "", "connection lost")
    job = Job(remote_job_id="123", state=STATE_RUNNING)
    with pytest.raises(TransportError):
        poll_runner(transport, host, [job])
    assert job.state == STATE_RUNNING
    with pytest.raises(TransportError):
        _start_runner(transport, host, "runner.sh", "Submitted")


@pytest.mark.parametrize("flavour", [remote_runner, remote_runner_ps])
def test_job_name_cannot_inject_a_script_line(flavour):
    script = flavour.build_job_script(
        "/jobs/a", "run.sh", "job.log", "abc", "/queue", job_name="name\nINJECTED COMMAND\r\n"
    )
    assert "\nINJECTED COMMAND" not in script
    assert "# MoleditPy job: name INJECTED COMMAND" in script


def test_exclusive_download_preserves_concurrently_created_file(tmp_path):
    target = tmp_path / "result.out"

    def download(remote, staging):
        Path(staging).write_bytes(b"download")
        target.write_bytes(b"user file")

    with pytest.raises(FileExistsError):
        _download_atomic(
            SimpleNamespace(download=download),
            "/remote/result",
            str(target),
            str(tmp_path),
            overwrite=False,
        )
    assert target.read_bytes() == b"user file"
    assert list(tmp_path.iterdir()) == [target]


def test_start_after_respects_utc_offset():
    expected = datetime.datetime(2026, 10, 24, 9, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert ApiInputs._start_after({"start_after": "2026-10-24T09:00:00Z"}) == expected
    assert ApiInputs._start_after({"start_after": "2026-10-24T18:00:00+09:00"}) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_do_not_enter_scheduling(value):
    with pytest.raises(ApiError):
        ApiInputs._start_after({"start_after": value})
    assert host_stats._first_number(str(value)) is None


@pytest.mark.parametrize("value", [True, 1.5, float("inf")])
def test_integer_api_does_not_truncate(value):
    with pytest.raises(ApiError):
        ApiInputs._int(value, "cores")


def test_malformed_collections_load_without_writing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"prefs": 42, "hosts": 1, "presets": True}), encoding="utf-8")
    jobs = tmp_path / "jobs.pmejbs"
    jobs.write_text(json.dumps({"jobs": 123}), encoding="utf-8")
    original = settings.read_bytes(), jobs.read_bytes()
    store = JobStore(str(tmp_path))
    assert not store.jobs and not store.hosts and not store.presets
    assert store.read_job_list(str(jobs)) == ([], False)
    assert (settings.read_bytes(), jobs.read_bytes()) == original
    assert Job.from_dict({"downloaded_files": [{"path": "bad"}]}).downloaded_files == []


def test_extreme_resource_numbers_do_not_crash_scanner():
    assert input_scan._int("9" * 400) == 0
    assert not input_scan.scan_text("%mem=" + "9" * 400 + "GB").found


def test_orca_scan_stays_inside_pal_block():
    found = input_scan.scan_text("%pal end\n# nprocs 128\n%maxcore 1000")
    assert found.cores == 0
    assert found.memory_mb == 1000


@pytest.mark.parametrize("flavour", [remote_runner, remote_runner_ps])
def test_dependency_header_rejects_newlines(flavour):
    with pytest.raises(ValueError, match="identifier"):
        flavour.build_job_script(
            "/jobs/a", "run.sh", "job.log", "abc", "/queue", after_job_id="first\necho injected"
        )


def test_saved_preset_retains_unknown_and_absent_fields():
    raw = {"modules": [3], "future_option": {"value": [1, 2]}}
    job = Job.from_dict({"preset": raw})
    assert job.preset == {"modules": [], "future_option": {"value": [1, 2]}}
    assert raw["modules"] == [3]
