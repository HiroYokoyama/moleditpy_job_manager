"""Transfers must not modify files outside their selected destination."""

import os
import subprocess
from unittest.mock import patch

import pytest

from job_manager import runner
from job_manager.transport.local import LocalTransport
from .fakes import make_host, make_job


@pytest.mark.skipif(os.name != "nt", reason="Windows drive containment")
def test_other_drive_is_outside_download_directory():
    assert not runner._inside_directory("C:\\results", "Z:\\outside")


@pytest.mark.parametrize("method", ["results", "host_paths"])
def test_predictable_staging_hardlink_does_not_truncate_other_file(tmp_path, method):
    remote = tmp_path / "remote"
    remote.mkdir()
    source = remote / "marker.out"
    source.write_text("downloaded")
    dest = tmp_path / "results"
    dest.mkdir()
    victim = tmp_path / "keep.txt"
    victim.write_text("keep me")
    os.link(victim, dest / ("marker.out" + runner.PARTIAL_SUFFIX))
    host = make_host(scheduler="windows" if os.name == "nt" else "slurm")
    transport = LocalTransport(host)
    if method == "results":
        with patch.object(runner, "list_remote_files", return_value=["marker.out"]):
            downloaded = runner.fetch_results(
                transport, make_job(remote_dir=str(remote)), str(dest)
            )
    else:
        with patch.object(runner, "stat_host_path", return_value={"type": "file", "exists": True}):
            downloaded, skipped = runner.download_host_paths(
                transport, host, [source.as_posix()], str(dest)
            )
        assert not skipped
    assert downloaded
    assert victim.read_text() == "keep me"
    assert (dest / "marker.out").read_text() == "downloaded"


def test_result_subdirectory_link_cannot_escape(tmp_path):
    remote = tmp_path / "remote"
    (remote / "scratch").mkdir(parents=True)
    (remote / "scratch/marker.out").write_text("downloaded")
    dest = tmp_path / "results"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "marker.out"
    victim.write_text("keep me")
    link = dest / "scratch"
    if os.name == "nt":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path $env:AUDIT_LINK_PATH -Target $env:AUDIT_TARGET_PATH | Out-Null",
            ],
            env={**os.environ, "AUDIT_LINK_PATH": str(link), "AUDIT_TARGET_PATH": str(outside)},
            check=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        transport = LocalTransport(make_host())
        with patch.object(runner, "list_remote_files", return_value=["scratch/marker.out"]):
            paths = runner.fetch_results(
                transport, make_job(remote_dir=str(remote)), str(dest), ["scratch/*.out"]
            )
        assert paths == []
        assert victim.read_text() == "keep me"
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()
