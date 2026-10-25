"""Failure boundaries discovered by reading submission and monitoring paths."""

import pytest

from job_manager import runner
from job_manager.schedulers import get_scheduler

from .fakes import FakeTransport, make_host, make_job, make_preset


@pytest.mark.parametrize("scheduler", ["slurm", "shell", "windows"])
def test_uploaded_input_cannot_be_overwritten_by_wrapper(tmp_path, scheduler):
    host = make_host(scheduler=scheduler)
    transport = FakeTransport(host).when("sbatch", stdout="4242\n")
    reserved = get_scheduler(scheduler).script_name
    path = tmp_path / reserved
    path.write_text("original input", encoding="utf-8")
    job = make_job(scheduler=scheduler, remote_job_id="")
    with pytest.raises(ValueError, match="(?i)(reserved|conflict|overwrite|wrapper)"):
        runner.submit_job(
            transport, host, make_preset(command_template="echo {input}"), job, [str(path)]
        )
    assert not transport.uploads


@pytest.mark.parametrize(
    "filename",
    ["job.log", "job.log.err", ".moleditpy_rc", ".moleditpy_rc.tmp", ".moleditpy_started"],
)
def test_relay_inputs_cannot_conflict_with_job_output_files(filename):
    job = make_job(log_file="job.log")
    with pytest.raises(ValueError, match="reserved"):
        runner.check_job_file_conflicts(job, get_scheduler("slurm"), [], [filename])


def test_windows_wrapper_conflicts_ignore_case():
    scheduler = get_scheduler("windows")
    job = make_job(scheduler="windows", log_file="job.log")
    with pytest.raises(ValueError, match="reserved"):
        runner.check_job_file_conflicts(job, scheduler, [], [scheduler.script_name.upper()])
