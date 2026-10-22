"""Job-list numbering and conversion happen without a migration write on read."""

import json
from pathlib import Path
from unittest.mock import patch

from job_manager.models import Job
from job_manager.store import JobStore, atomic_write_json


def legacy_file(path):
    atomic_write_json(
        str(path),
        {
            "version": 1,
            "jobs": [
                {"id": "newer", "submitted_at": 20, "updated_at": 20},
                {"id": "older", "submitted_at": 10, "updated_at": 10},
            ],
        },
    )


def test_loading_and_reloading_v1_never_saves_it(tmp_path):
    source = tmp_path / "jobs.pmejbs"
    legacy_file(source)
    original = source.read_bytes()
    modified = source.stat().st_mtime_ns
    with patch("job_manager.store.atomic_write_json") as write:
        store = JobStore(str(tmp_path))
        assert store.jobs["older"].number == 1
        assert store.jobs["newer"].number == 2
        store.reload_jobs()
        store.read_job_list(str(source))
        store.use_jobs_file(str(source))
        write.assert_not_called()
    assert source.read_bytes() == original
    assert source.stat().st_mtime_ns == modified


def test_explicit_save_converts_v1_to_v2_and_round_trips_numbers(tmp_path):
    source = tmp_path / "jobs.pmejbs"
    legacy_file(source)
    store = JobStore(str(tmp_path))
    store.save_jobs()
    payload = json.loads(source.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert {job["id"]: job["number"] for job in payload["jobs"]} == {"older": 1, "newer": 2}
    reopened = JobStore(str(tmp_path))
    assert {job.id: job.number for job in reopened.job_list()} == {"older": 1, "newer": 2}


def test_save_as_and_archive_write_v2_without_rewriting_source(tmp_path):
    source = tmp_path / "old.pmejbs"
    legacy_file(source)
    original = source.read_bytes()
    store = JobStore(str(tmp_path))
    store.use_jobs_file(str(source))
    target = tmp_path / "new.pmejbs"
    store.write_job_list(str(target), store.job_list(), reconstructed=True)
    archive = Path(store.archive_jobs())
    for path in (target, archive):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 2
        assert {job["id"]: job["number"] for job in payload["jobs"]} == {"older": 1, "newer": 2}
    assert json.loads(target.read_text(encoding="utf-8"))["reconstructed"] is True
    assert json.loads(archive.read_text(encoding="utf-8"))["archived"] is True
    assert source.read_bytes() == original


def test_merged_jobs_from_other_windows_have_unique_chronological_numbers(tmp_path):
    store = JobStore(str(tmp_path))
    store.add_job(Job(id="a", submitted_at=10))
    other = JobStore(str(tmp_path))
    other.add_job(Job(id="b", submitted_at=20))
    store.add_job(Job(id="c", submitted_at=30))
    payload = json.loads(Path(store.jobs_path).read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert {job["id"]: job["number"] for job in payload["jobs"]} == {"a": 1, "b": 2, "c": 3}


def test_settings_schema_is_unchanged(tmp_path):
    store = JobStore(str(tmp_path))
    store.save_settings()
    assert json.loads(Path(store.settings_path).read_text(encoding="utf-8"))["version"] == 1
