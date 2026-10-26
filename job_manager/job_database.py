"""Single-file SQLite job lists, with short-lived connections and delta writes."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path

from .models import Job, assign_job_numbers

EXTENSION = ".pmejdb"
APPLICATION_ID = 0x504D4A44  # PMJD
SCHEMA_VERSION = 1
_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE jobs (id TEXT PRIMARY KEY, record TEXT NOT NULL, revision INTEGER NOT NULL)",
)
_STATE_FIELDS = frozenset(
    {"state", "rc", "started_at", "finished_at", "last_server_state", "updated_at"}
)


def is_database(path: str) -> bool:
    return str(path).lower().endswith(EXTENSION)


@contextmanager
def connection(path: str, *, write=False, exclusive=False):
    """Reading never creates a file; all writers serialize before reading rows.

    DELETE journaling deliberately leaves no WAL sidecar to forget when sharing
    a closed file. A database in use should be exported rather than copied.
    """
    target = Path(path).absolute()
    created = False
    committed = False
    db = None
    try:
        if write and (exclusive or not target.exists()):
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if exclusive:
                    raise
            else:
                os.close(descriptor)
                created = True
        db = sqlite3.connect(
            target.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            timeout=2,
            isolation_level=None,
        )
        db.execute("PRAGMA trusted_schema=OFF")
        if write:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
        else:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
        if created:
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            for statement in _SCHEMA:
                db.execute(statement)
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("This file is not a MoleditPy job database")
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Unsupported job database schema; update Job Manager")
        objects = {
            (kind, name)
            for kind, name in db.execute(
                "SELECT type,name FROM sqlite_master WHERE substr(name,1,7) != 'sqlite_'"
            )
        }
        if objects != {("table", "metadata"), ("table", "jobs")}:
            raise ValueError("Unexpected objects in job database")
        # Reject altered tables before any DELETE/UPDATE can execute their SQL.
        for name, statement in zip(("metadata", "jobs"), _SCHEMA):
            actual = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            if actual != statement:
                raise ValueError("Unsupported job database table definition")
        yield db
        db.commit()
        committed = True
    except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
        raise OSError(f"Could not access job database {target}: {error}") from error
    finally:
        if db is not None:
            db.close()  # Also rolls back an unsuccessful transaction.
        if created and not committed and target.exists():
            # A failed creation must not leave an empty file hiding a legacy list.
            target.unlink()


def _document(db):
    document = {
        key: json.loads(value) for key, value in db.execute("SELECT key,value FROM metadata")
    }
    records = []
    for identifier, value in db.execute("SELECT id,record FROM jobs ORDER BY rowid"):
        record = json.loads(value)
        if not isinstance(record, dict) or record.get("id") != identifier or not identifier:
            raise ValueError("Invalid job database record")
        records.append(record)
    document["jobs"] = records
    return document


def read_document(path: str):
    with connection(path) as db:
        return _document(db)


def _metadata(db, document):
    current = dict(db.execute("SELECT key,value FROM metadata"))
    for key, value in document.items():
        if key == "jobs":
            continue
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if current.get(key) != encoded:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, encoded))


def write_document(path: str, document, *, exclusive=False):
    """An explicit Save As snapshot, replacing rows only inside a transaction."""
    with connection(path, write=True, exclusive=exclusive) as db:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM metadata")
        _metadata(db, document)
        for record in document["jobs"]:
            db.execute(
                "INSERT INTO jobs VALUES (?,?,1)",
                (record["id"], json.dumps(record, ensure_ascii=False)),
            )


def _merge(base, mine, theirs):
    """Preserve unrelated edits; a conflicting field keeps the committed value.

    Scheduler state fields travel together. For concurrent observations, their
    updated_at timestamps choose that group, rather than mixing exit codes and
    states from different observations.
    """
    result = dict(theirs)
    local_state = any(mine.get(key) != base.get(key) for key in _STATE_FIELDS)
    remote_state = any(theirs.get(key) != base.get(key) for key in _STATE_FIELDS)
    for key, value in mine.items():
        if key == "number" or value == base.get(key):
            continue
        if key in _STATE_FIELDS:
            continue
        if theirs.get(key) == base.get(key) or value == theirs.get(key):
            result[key] = value
        else:
            logging.warning("Job Manager: concurrent edit to job field %s; kept saved value", key)
    if local_state and (
        not remote_state or mine.get("updated_at", 0) > theirs.get("updated_at", 0)
    ):
        for key in _STATE_FIELDS:
            if key in mine:
                result[key] = mine[key]
    return result


def save_changes(path: str, document, baseline: dict, forgotten: set):
    """Commit only local changes, preserving other windows' updates/removals."""
    with connection(path, write=True) as db:
        disk_document = _document(db)
        before = {row["id"]: deepcopy(row) for row in disk_document["jobs"]}
        disk = {row["id"]: row for row in disk_document["jobs"]}
        for identifier in forgotten:
            disk.pop(identifier, None)
        for mine in document["jobs"]:
            identifier = mine["id"]
            if identifier in forgotten:
                continue
            base = baseline.get(identifier)
            theirs = disk.get(identifier)
            if base is not None and theirs is None:
                continue  # A stale window must not resurrect a removed job.
            if theirs is None:
                disk[identifier] = mine
            elif base is not None:
                disk[identifier] = _merge(base, mine, theirs)
            elif mine.get("updated_at", 0) >= theirs.get("updated_at", 0):
                disk[identifier] = mine
        jobs = [Job.from_dict(raw) for raw in disk.values()]
        assign_job_numbers(jobs)
        for raw, job in zip(disk.values(), jobs):
            raw["number"] = job.number
        for identifier in before.keys() - disk.keys():
            db.execute("DELETE FROM jobs WHERE id=?", (identifier,))
        for identifier, raw in disk.items():
            if before.get(identifier) == raw:
                continue
            encoded = json.dumps(raw, ensure_ascii=False)
            db.execute(
                "INSERT INTO jobs VALUES (?,?,1) ON CONFLICT(id) DO UPDATE SET "
                "record=excluded.record, revision=jobs.revision+1",
                (identifier, encoded),
            )
        _metadata(db, document)
        return disk
