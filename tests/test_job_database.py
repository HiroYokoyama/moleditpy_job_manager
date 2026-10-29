"""Real SQLite transactions and separate writers, using synthetic records only."""

import sqlite3
import os
import threading
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import pytest

from job_manager import job_database as database
from job_manager.models import Job


def document(*records):
    return {"version": 2, "archived": False, "reconstructed": False, "jobs": list(records)}


def job(identifier="synthetic", **fields):
    return Job(id=identifier, updated_at=100, **fields).to_dict()


def test_single_file_snapshot_round_trips_text_and_flags(tmp_path):
    path = tmp_path / "shared.pmejdb"
    original = document(job(command="echo '日本語'\necho second", preset={"extra": [1, None]}))
    original["archived"] = True
    database.write_document(str(path), original)
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    assert database.read_document(str(path)) == original
    assert list(tmp_path.iterdir()) == [path]


def test_read_missing_or_foreign_database_never_creates_or_changes_it(tmp_path):
    path = tmp_path / "foreign.pmejdb"
    with pytest.raises(OSError):
        database.read_document(str(path))
    assert not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value)")
    before = path.read_bytes()
    for operation in (database.read_document, lambda p: database.write_document(p, document())):
        with pytest.raises(OSError, match="not a MoleditPy"):
            operation(str(path))
    assert path.read_bytes() == before


def test_failed_snapshot_rolls_back_existing_file(tmp_path):
    path = str(tmp_path / "atomic.pmejdb")
    original = document(job())
    database.write_document(path, original)
    with pytest.raises(OSError):
        database.write_document(path, document(job("duplicate"), job("duplicate")))
    assert database.read_document(path) == original


def test_failed_creation_does_not_hide_a_legacy_list(tmp_path):
    path = tmp_path / "failed.pmejdb"
    with pytest.raises(OSError):
        database.write_document(str(path), document({"not": "a job"}))
    assert not path.exists()


@pytest.mark.parametrize("initialized", [False, True])
def test_parallel_writers_keep_every_new_job(tmp_path, initialized):
    path = str(tmp_path / "parallel.pmejdb")
    if initialized:
        database.write_document(path, document())

    def write(index):
        return database.save_changes(path, document(job(str(index))), {}, set())

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    records = database.read_document(path)["jobs"]
    assert {row["id"] for row in records} == {str(i) for i in range(12)}
    assert len({row["number"] for row in records}) == 12


def test_a_writer_waits_out_a_lock_held_past_two_seconds(tmp_path):
    # A 2 s busy timeout lost a save on a Windows runner while four writers
    # queued behind each other's fsyncs. Hold the lock longer than that.
    path = str(tmp_path / "held.pmejdb")
    database.write_document(path, document())
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    released = threading.Timer(2.5, holder.commit)
    released.start()
    try:
        database.save_changes(path, document(job("late")), {}, set())
    finally:
        released.join()
        holder.close()
    assert [row["id"] for row in database.read_document(path)["jobs"]] == ["late"]


def test_stale_writer_preserves_updates_and_deletions(tmp_path):
    path = str(tmp_path / "stale.pmejdb")
    old = job(state="RUNNING")
    database.write_document(path, document(old))
    newer = dict(old, state="DONE", rc=0, updated_at=200)
    database.save_changes(path, document(newer), {old["id"]: old}, set())
    database.save_changes(path, document(old, job("new")), {old["id"]: old}, set())
    records = {row["id"]: row for row in database.read_document(path)["jobs"]}
    assert records[old["id"]]["state"] == "DONE"
    database.save_changes(path, document(), records, {old["id"]})
    database.save_changes(path, document(old), {old["id"]: old}, set())
    assert {row["id"] for row in database.read_document(path)["jobs"]} == {"new"}


def test_disjoint_edits_merge_and_conflicting_field_keeps_saved_value(tmp_path, caplog):
    path = str(tmp_path / "merge.pmejdb")
    old = job(name="initial", command="echo original")
    database.write_document(path, document(old))
    baseline = {old["id"]: old}
    database.save_changes(path, document(dict(old, name="other")), baseline, set())
    database.save_changes(path, document(dict(old, command="echo changed")), baseline, set())
    database.save_changes(path, document(dict(old, name="stale")), baseline, set())
    saved = database.read_document(path)["jobs"][0]
    assert saved["name"] == "other"
    assert saved["command"] == "echo changed"
    assert "concurrent edit" in caplog.text


def test_unknown_schema_and_triggers_are_rejected(tmp_path):
    path = str(tmp_path / "untrusted.pmejdb")
    database.write_document(path, document(job()))
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER sqlitex_attack AFTER UPDATE ON jobs BEGIN DELETE FROM jobs; END")
    with pytest.raises(OSError, match="Unexpected objects"):
        database.save_changes(path, document(job("new")), {}, set())


def test_newer_schema_is_never_overwritten(tmp_path):
    path = str(tmp_path / "future.pmejdb")
    database.write_document(path, document(job()))
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(OSError, match="Unsupported job database schema"):
        database.write_document(path, document())


def test_timestamp_only_edit_does_not_claim_a_stale_scheduler_state(tmp_path):
    path = str(tmp_path / "timestamp.pmejdb")
    old = job(state="RUNNING", name="original")
    database.write_document(path, document(old))
    baseline = {old["id"]: old}
    database.save_changes(
        path, document(dict(old, state="DONE", rc=0, updated_at=200)), baseline, set()
    )
    database.save_changes(
        path, document(dict(old, name="renamed", updated_at=300)), baseline, set()
    )
    saved = database.read_document(path)["jobs"][0]
    assert (saved["state"], saved["rc"], saved["name"]) == ("DONE", 0, "renamed")


def test_exclusive_snapshot_never_overwrites_a_target(tmp_path):
    path = str(tmp_path / "exists.pmejdb")
    database.write_document(path, document(job()))
    with pytest.raises(FileExistsError):
        database.write_document(path, document(), exclusive=True)
    assert len(database.read_document(path)["jobs"]) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows rename has no-replace semantics")
def test_new_windows_database_does_not_require_hard_links(tmp_path):
    path = str(tmp_path / "removable.pmejdb")
    with patch("job_manager.job_database.os.link", side_effect=OSError("No hard links")):
        database.write_document(path, document(job()))
    assert len(database.read_document(path)["jobs"]) == 1
