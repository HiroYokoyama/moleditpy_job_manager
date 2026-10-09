"""Check the interfaces between persistence, validation, routing and execution."""

import importlib
import os
import subprocess
import sys

import pytest

from job_manager.api_core import ApiError, JobApi
from job_manager.models import HostProfile, Job, SCHEDULER_WINDOWS, STATE_PENDING
from job_manager.runner import submit_job, submit_to_runner
from job_manager.store import JobStore

from .api_support import FakeService
from .fakes import FakeTransport, make_host, make_job, make_preset


def test_non_gui_components_import_without_qt():
    script = """
import importlib
import sys

class NoQt:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PyQt6' or fullname.startswith('PyQt6.'):
            raise ImportError('Qt must remain optional for these components')

sys.meta_path.insert(0, NoQt())
for name in ('api_core', 'api_inputs', 'api_security', 'api_types', 'input_names',
             'job_dependencies', 'store', 'store_io', 'runner', 'result_files'):
    importlib.import_module('job_manager.' + name)
assert not any(name.startswith('PyQt6') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scheduler", ["slurm", SCHEDULER_WINDOWS])
def test_api_refuses_upload_collisions_before_creating_a_job(tmp_path, scheduler):
    store = JobStore(str(tmp_path / "store"))
    host = HostProfile(name="myhost", scheduler=scheduler)
    store.add_host(host)
    service = FakeService(store)
    paths = []
    for directory in ("first", "second"):
        path = tmp_path / directory / "input.inp"
        path.parent.mkdir()
        path.write_text("input", encoding="utf-8")
        paths.append(str(path))
    with pytest.raises(ApiError) as error:
        JobApi(service).submit({"host": host.id, "files": paths, "command": "mycommand {input}"})
    assert error.value.status == 400
    assert "overwrite" in str(error.value)
    assert not service.submitted
    assert not store.jobs


@pytest.mark.parametrize("submit", [submit_job, submit_to_runner])
@pytest.mark.parametrize("scheduler", ["slurm", SCHEDULER_WINDOWS])
def test_direct_runner_refuses_collisions_before_any_remote_writes(submit, scheduler):
    host = make_host(scheduler=scheduler)
    transport = FakeTransport(host)
    job = make_job(scheduler=scheduler)
    paths = [os.path.join("first", "input.inp"), os.path.join("second", "input.inp")]
    with pytest.raises(ValueError, match="overwrite"):
        submit(transport, host, make_preset(), job, paths)
    assert not transport.commands
    assert not transport.uploads


def test_windows_runner_refuses_case_only_upload_collisions():
    host = make_host(scheduler=SCHEDULER_WINDOWS)
    transport = FakeTransport(host)
    with pytest.raises(ValueError, match="overwrite"):
        submit_to_runner(
            transport,
            host,
            make_preset(),
            make_job(scheduler=SCHEDULER_WINDOWS),
            [os.path.join("first", "INPUT.inp"), os.path.join("second", "input.inp")],
        )
    assert not transport.uploads


def test_replacing_loaded_jobs_does_not_leave_dependency_views_stale(tmp_path):
    store = JobStore(str(tmp_path))
    old = Job(host_id="myhost", state=STATE_PENDING, submitted_at=1)
    new = Job(host_id="myhost", state=STATE_PENDING, submitted_at=2)
    store.jobs = {old.id: old}
    assert store.chain_tail("myhost") is old
    store.jobs = {new.id: new}
    assert store.chain_tail("myhost") is new
    assert store.runnable_jobs("myhost") == [new]


@pytest.mark.parametrize(
    "facade,component,name",
    [
        ("api_core", "api_types", "ApiError"),
        ("api_core", "api_types", "Deferred"),
        ("api_core", "api_security", "tokens_match"),
        ("runner", "result_files", "fetch_results"),
        ("runner", "input_names", "check_input_name"),
        ("store", "store_io", "atomic_write_json"),
    ],
)
def test_existing_imports_keep_the_shared_type_and_function_identity(facade, component, name):
    original = importlib.import_module("job_manager." + facade)
    owner = importlib.import_module("job_manager." + component)
    assert getattr(original, name) is getattr(owner, name)
