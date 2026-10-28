"""Explicit conversion and portable saves never touch a real user's job list."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from job_manager.job_database import read_document
from job_manager.models import Job
from job_manager.store import JobStore, atomic_write_json


def legacy_store(tmp_path, extension=".pmejbs"):
    path = tmp_path / ("jobs" + extension)
    atomic_write_json(str(path), {"version": 2, "jobs": [Job(id="synthetic").to_dict()]})
    return JobStore(str(tmp_path)), path


def test_fresh_store_uses_sqlite_without_creating_it_on_read(tmp_path):
    store = JobStore(str(tmp_path))
    assert store.jobs_path.endswith("jobs.pmejdb")
    assert not Path(store.jobs_path).exists()
    store.add_job(Job(id="synthetic"))
    assert read_document(store.jobs_path)["jobs"][0]["id"] == "synthetic"
    assert not (tmp_path / "jobs.pmejbs").exists()


def test_database_reads_and_unchanged_save_do_not_rewrite_file(tmp_path):
    store = JobStore(str(tmp_path))
    store.add_job(Job(id="synthetic"))
    path = Path(store.jobs_path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    reopened = JobStore(str(tmp_path))
    reopened.reload_jobs()
    reopened.read_job_list(str(path))
    reopened.read_job_flags(str(path))
    reopened.save_jobs()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("extension", [".pmejbs", ".json"])
def test_loading_and_saving_legacy_never_implicitly_converts(tmp_path, extension):
    store, path = legacy_store(tmp_path, extension)
    original = path.read_bytes()
    store.reload_jobs()
    assert path.read_bytes() == original
    store.add_job(Job(id="another"))
    assert len(json.loads(path.read_text("utf-8"))["jobs"]) == 2
    assert not path.with_suffix(".pmejdb").exists()


@pytest.mark.parametrize("extension", [".pmejbs", ".json"])
def test_explicit_conversion_archives_original_and_switches_default(tmp_path, extension):
    store, path = legacy_store(tmp_path, extension)
    original = path.read_bytes()
    assert store.can_convert_jobs()
    target, archive = store.convert_jobs()
    assert Path(archive).read_bytes() == original
    assert not path.exists()
    assert target == str(path.with_suffix(".pmejdb"))
    assert store.jobs_path == target
    assert store.using_default_jobs_file()
    assert not store.can_convert_jobs()
    assert JobStore(str(tmp_path)).jobs_path == target
    assert set(JobStore(str(tmp_path)).jobs) == {"synthetic"}
    assert store.read_job_list(archive)[1] is True


def test_converting_external_list_leaves_default_unchanged(tmp_path):
    store = JobStore(str(tmp_path))
    store.add_job(Job(id="default"))
    default = Path(store.jobs_path).read_bytes()
    external = tmp_path / "other.pmejbs"
    store.write_job_list(str(external), [Job(id="external")], reconstructed=True)
    store.use_jobs_file(str(external))
    target, archive = store.convert_jobs()
    assert not store.using_default_jobs_file()
    assert Path(store.default_jobs_path).read_bytes() == default
    assert store.read_job_flags(target)["reconstructed"]
    assert Path(archive).exists()


def test_converter_refuses_existing_target_without_changing_anything(tmp_path):
    store, source = legacy_store(tmp_path)
    target = source.with_suffix(".pmejdb")
    store.export_jobs(str(target))
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        store.convert_jobs()
    assert source.exists()
    assert target.read_bytes() == before
    assert store.archived_files() == []


def test_failed_backup_keeps_legacy_active_without_publishing_db(tmp_path):
    store, source = legacy_store(tmp_path)
    original = source.read_bytes()
    with patch("job_manager.store.os.fsync", side_effect=OSError("synthetic disk full")):
        with pytest.raises(OSError):
            store.convert_jobs()
    assert source.read_bytes() == original
    assert not source.with_suffix(".pmejdb").exists()
    assert store.jobs_path == str(source)


def test_legacy_window_cannot_recreate_the_converted_source(tmp_path):
    store, source = legacy_store(tmp_path)
    other = JobStore(str(tmp_path))
    store.convert_jobs()
    with pytest.raises(OSError, match="moved or converted"):
        other.save_jobs()
    assert not source.exists()


def test_portable_save_as_both_formats_and_reopen(tmp_path):
    store = JobStore(str(tmp_path / "data"))
    store.add_job(Job(id="synthetic", command="echo 'synthetic'\necho next"))
    for extension in (".pmejdb", ".pmejbs"):
        path = tmp_path / ("shared" + extension)
        store.export_jobs(str(path))
        other = JobStore(str(tmp_path / "recipient"))
        other.use_jobs_file(str(path))
        assert other.jobs["synthetic"].command == store.jobs["synthetic"].command
        other.add_job(Job(id="recipient"))
        assert {job.id for job in store.read_job_list(str(path))[0]} == {"synthetic", "recipient"}


def test_save_as_to_live_database_does_not_discard_other_windows_jobs(tmp_path):
    store = JobStore(str(tmp_path))
    store.add_job(Job(id="mine"))
    other = JobStore(str(tmp_path))
    other.add_job(Job(id="other"))
    store.export_jobs(store.jobs_path)
    assert set(JobStore(str(tmp_path)).jobs) == {"mine", "other"}


def test_stale_window_cannot_rewind_saved_state_on_unrelated_save(tmp_path):
    store = JobStore(str(tmp_path))
    store.add_job(Job(id="shared", state="RUNNING"))
    other = JobStore(str(tmp_path))
    other.jobs["shared"].touch("DONE")
    other.jobs["shared"].rc = 0
    other.save_jobs()
    store.add_job(Job(id="unrelated"))
    assert JobStore(str(tmp_path)).jobs["shared"].state == "DONE"


def test_conversion_refuses_transfers(tmp_path):
    store, source = legacy_store(tmp_path)
    store.jobs["synthetic"].state = "UPLOADING"
    with pytest.raises(OSError, match="uploads and downloads"):
        store.convert_jobs()
    assert source.exists()


def test_conversion_does_not_carry_configuration_from_a_legacy_document(tmp_path):
    store, source = legacy_store(tmp_path)
    payload = json.loads(source.read_text("utf-8"))
    payload["hosts"] = [{"password": "synthetic-not-a-real-secret"}]
    atomic_write_json(str(source), payload)
    target, archive = store.convert_jobs()
    assert "hosts" not in read_document(target)
    # The preserved original is kept locally, byte for byte.
    assert "hosts" in json.loads(Path(archive).read_text("utf-8"))


def test_duplicate_legacy_ids_are_not_silently_lost_during_conversion(tmp_path):
    store, source = legacy_store(tmp_path)
    payload = json.loads(source.read_text("utf-8"))
    payload["jobs"].append(dict(payload["jobs"][0], name="different record"))
    atomic_write_json(str(source), payload)
    before = source.read_bytes()
    with pytest.raises(OSError, match="Could not read"):
        store.convert_jobs()
    assert source.read_bytes() == before
    assert not source.with_suffix(".pmejdb").exists()


def test_another_live_tracker_prevents_false_interrupted_transfer_recovery(tmp_path):
    store = JobStore(str(tmp_path))
    store.add_job(Job(id="synthetic", state="UPLOADING"))
    from job_manager import instances

    server = instances.LivenessServer()
    try:
        with patch("job_manager.instances.os.getpid", return_value=999999):
            instances.register_instance(str(tmp_path), "moleditpy", time.time(), server)
        # PID 999999 is not a real process; only the socket decides here.
        with patch("job_manager.instances._pid_running", return_value=True):
            assert JobStore(str(tmp_path)).jobs["synthetic"].state == "UPLOADING"
            server.close()
            assert JobStore(str(tmp_path)).jobs["synthetic"].state == "FAILED"
    finally:
        server.close()
