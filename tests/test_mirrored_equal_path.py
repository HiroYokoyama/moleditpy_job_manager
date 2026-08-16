"""Tests for equal path local mirror resolution and file listing."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from job_manager.models import HostProfile, Job
from job_manager.output_file_dialog import OutputFileSelectorDialog
from job_manager.service import JobService
from job_manager.store import JobStore


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def temp_store(tmp_path):
    return JobStore(str(tmp_path))


@pytest.fixture
def service(temp_store):
    return JobService(temp_store)


def test_mirrored_job_dir_resolution(service, temp_store, tmp_path):
    mirror_root = str(tmp_path / "mirror")
    job_mirror = os.path.join(mirror_root, "job_123")
    os.makedirs(job_mirror, exist_ok=True)
    out_file = os.path.join(job_mirror, "output.out")
    with open(out_file, "w") as f:
        f.write("Calculation done\n")

    host = HostProfile(
        id="h_mirror",
        name="Host Mirror",
        remote_root="/home/user/jobs",
        equal_path=mirror_root,
    )
    temp_store.add_host(host)

    job = Job(
        id="j_mirror",
        name="job_123",
        host_id="h_mirror",
        remote_dir="/home/user/jobs/job_123",
    )

    dlg = OutputFileSelectorDialog(service, job)
    assert dlg._mirrored_job_dir() == job_mirror

    existing = dlg._get_existing_local_files()
    assert os.path.normpath(out_file) in [os.path.normpath(p) for p in existing]
