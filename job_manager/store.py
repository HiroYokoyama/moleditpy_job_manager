"""Persistence for host profiles, submit presets and the job database.

Both files live in ``~/.moleditpy/job_manager/`` -- *outside* the plugin folder.
The Plugin Installer replaces the whole package directory on update and carries
over only a file literally named ``settings.json``, so anything kept beside the
code (``jobs.json`` in particular) would be silently destroyed. A directory
under the user's MoleditPy home survives updates, reinstalls and
"Reset All Settings" alike.

* ``settings.json`` -- hosts, presets and preferences.
* ``jobs.json`` -- the tracked jobs. Global on purpose; HPC jobs outlive both
  the open project and the application session.

Both are written atomically (temp file in the same directory + ``os.replace``)
so a crash mid-write can never leave a truncated JSON behind.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

from .models import (
    TERMINAL_STATES,
    HostProfile,
    Job,
    SubmitPreset,
)

SETTINGS_FILENAME = "settings.json"
JOBS_FILENAME = "jobs.json"

DEFAULT_POLL_INTERVAL = 120
#: Absolute floor. Low enough for a local test host or a short debug job;
#: anything faster is a busy loop, not a status check.
MIN_POLL_INTERVAL = 5
#: Below this the user is warned. A shared login node charges every `squeue`
#: to the whole cluster, and site admins do notice.
RECOMMENDED_MIN_POLL_INTERVAL = 30
MAX_POLL_INTERVAL = 3600
DEFAULT_PRUNE_DAYS = 90

DEFAULT_PREFS: Dict[str, Any] = {
    "poll_interval": DEFAULT_POLL_INTERVAL,
    "prune_days": DEFAULT_PRUNE_DAYS,
    "download_root": "",
    "open_result_after_download": True,
    "last_input_dir": "",
    #: The user's own command templates: [{"label": ..., "command": ...}].
    "command_templates": [],
}


#: Overridable so tests never touch the real user directory.
DATA_DIR_ENV = "MOLEDITPY_JOB_MANAGER_DIR"


def default_data_dir() -> str:
    """``~/.moleditpy/job_manager`` -- outside the replaceable plugin folder."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".moleditpy", "job_manager")


def default_download_root() -> str:
    """Where fetched results land unless the user picks somewhere else."""
    return os.path.join(default_data_dir(), "downloads")


def atomic_write_json(path: str, data: Any) -> None:
    """Serialize ``data`` to ``path`` without ever leaving a partial file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".tmp_",
        suffix=".json",
        delete=False,
    )
    tmp_path = handle.name
    try:
        with handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            logging.debug("Job Manager: could not remove temp file %s", tmp_path)
        raise


def read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        logging.warning("Job Manager: could not read %s; using defaults", path)
        return default


class JobStore:
    """In-memory model of hosts/presets/jobs, backed by two JSON files."""

    def __init__(self, directory: Optional[str] = None) -> None:
        self.directory = directory or default_data_dir()
        self.settings_path = os.path.join(self.directory, SETTINGS_FILENAME)
        self.jobs_path = os.path.join(self.directory, JOBS_FILENAME)
        self.hosts: Dict[str, HostProfile] = {}
        self.presets: Dict[str, SubmitPreset] = {}
        self.jobs: Dict[str, Job] = {}
        self.prefs: Dict[str, Any] = dict(DEFAULT_PREFS)
        self.load()

    # --- loading / saving ---------------------------------------------------

    def load(self) -> None:
        settings = read_json(self.settings_path, {}) or {}
        self.prefs = dict(DEFAULT_PREFS)
        self.prefs.update(settings.get("prefs") or {})
        self.hosts = {}
        for raw in settings.get("hosts") or []:
            host = HostProfile.from_dict(raw)
            self.hosts[host.id] = host
        self.presets = {}
        for raw in settings.get("presets") or []:
            preset = SubmitPreset.from_dict(raw)
            self.presets[preset.id] = preset

        jobs_doc = read_json(self.jobs_path, {}) or {}
        self.jobs = {}
        for raw in jobs_doc.get("jobs") or []:
            job = Job.from_dict(raw)
            self.jobs[job.id] = job

    def save_settings(self) -> None:
        atomic_write_json(
            self.settings_path,
            {
                "version": 1,
                "prefs": self.prefs,
                "hosts": [h.to_dict() for h in self.hosts.values()],
                "presets": [p.to_dict() for p in self.presets.values()],
            },
        )

    def save_jobs(self) -> None:
        atomic_write_json(
            self.jobs_path,
            {"version": 1, "jobs": [j.to_dict() for j in self.jobs.values()]},
        )

    # --- hosts --------------------------------------------------------------

    def add_host(self, host: HostProfile) -> HostProfile:
        self.hosts[host.id] = host
        self.save_settings()
        return host

    def remove_host(self, host_id: str) -> None:
        self.hosts.pop(host_id, None)
        for preset_id in [p.id for p in self.presets.values() if p.host_id == host_id]:
            self.presets.pop(preset_id, None)
        self.save_settings()

    def host_list(self) -> List[HostProfile]:
        return sorted(self.hosts.values(), key=lambda h: h.name.lower())

    # --- presets ------------------------------------------------------------

    def add_preset(self, preset: SubmitPreset) -> SubmitPreset:
        self.presets[preset.id] = preset
        self.save_settings()
        return preset

    def remove_preset(self, preset_id: str) -> None:
        self.presets.pop(preset_id, None)
        self.save_settings()

    def presets_for_host(self, host_id: str) -> List[SubmitPreset]:
        return sorted(
            (p for p in self.presets.values() if p.host_id == host_id),
            key=lambda p: p.name.lower(),
        )

    # --- jobs ---------------------------------------------------------------

    def add_job(self, job: Job) -> Job:
        self.jobs[job.id] = job
        self.save_jobs()
        return job

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        self.save_jobs()

    def job_list(self) -> List[Job]:
        return sorted(
            self.jobs.values(), key=lambda j: j.submitted_at or j.updated_at, reverse=True
        )

    def active_jobs(self) -> List[Job]:
        return [j for j in self.jobs.values() if j.is_active]

    def active_jobs_by_host(self) -> Dict[str, List[Job]]:
        grouped: Dict[str, List[Job]] = {}
        for job in self.active_jobs():
            grouped.setdefault(job.host_id, []).append(job)
        return grouped

    def prune(self, days: Optional[int] = None) -> int:
        """Drop terminal jobs older than ``days``. Returns the number removed."""
        limit_days = self.prefs.get("prune_days", DEFAULT_PRUNE_DAYS) if days is None else days
        if not limit_days or limit_days <= 0:
            return 0
        cutoff = time.time() - float(limit_days) * 86400.0
        stale = [
            job.id
            for job in self.jobs.values()
            if job.state in TERMINAL_STATES and (job.finished_at or job.updated_at) < cutoff
        ]
        for job_id in stale:
            self.jobs.pop(job_id, None)
        if stale:
            self.save_jobs()
        return len(stale)

    # --- prefs --------------------------------------------------------------

    def get_pref(self, key: str, default: Any = None) -> Any:
        return self.prefs.get(key, DEFAULT_PREFS.get(key, default))

    def set_pref(self, key: str, value: Any) -> None:
        self.prefs[key] = value
        self.save_settings()

    # --- user command templates ---------------------------------------------

    def user_templates(self) -> List[Dict[str, str]]:
        """The user's own command templates, as ``{"label", "command"}`` dicts."""
        raw = self.get_pref("command_templates", []) or []
        return [
            {"label": str(item.get("label", "")), "command": str(item.get("command", ""))}
            for item in raw
            if isinstance(item, dict) and item.get("label")
        ]

    def add_user_template(self, label: str, command: str) -> None:
        """Save (or replace) one template. Persisted in settings.json."""
        label = (label or "").strip()
        if not label:
            return
        templates = [t for t in self.user_templates() if t["label"] != label]
        templates.append({"label": label, "command": command or ""})
        self.set_pref("command_templates", sorted(templates, key=lambda t: t["label"].lower()))

    def remove_user_template(self, label: str) -> None:
        self.set_pref(
            "command_templates", [t for t in self.user_templates() if t["label"] != label]
        )

    def download_root(self) -> str:
        configured = str(self.get_pref("download_root", "") or "").strip()
        return os.path.expanduser(configured) if configured else default_download_root()

    @property
    def poll_interval(self) -> int:
        """Effective poll interval, clamped to the permitted range."""
        try:
            value = int(self.get_pref("poll_interval", DEFAULT_POLL_INTERVAL))
        except (TypeError, ValueError):
            value = DEFAULT_POLL_INTERVAL
        return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, value))

    @property
    def poll_interval_is_aggressive(self) -> bool:
        """True when the interval is fast enough to be worth warning about."""
        return self.poll_interval < RECOMMENDED_MIN_POLL_INTERVAL
