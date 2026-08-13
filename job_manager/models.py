"""Data model for hosts, submit presets and tracked jobs.

Pure Python: no Qt, no RDKit, no network. Everything here must stay importable
with nothing but the standard library so the headless test suite (and CI, which
installs only pytest) can exercise it directly.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

# --- Canonical job states -------------------------------------------------
# Kept as plain strings so they round-trip through JSON without a codec.

STATE_NEW = "NEW"
STATE_UPLOADING = "UPLOADING"
STATE_SUBMITTED = "SUBMITTED"
STATE_PENDING = "PENDING"
STATE_RUNNING = "RUNNING"
STATE_COMPLETING = "COMPLETING"
STATE_DOWNLOADING = "DOWNLOADING"
STATE_DONE = "DONE"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"
STATE_LOST = "LOST"
#: Display only: a chained job whose predecessor has not finished. The wrapper
#: is alive and waiting, so the queue reports it running -- but "RUNNING" would
#: tell the user their calculation had started when it has not.
STATE_QUEUED = "QUEUED"
#: Display only: a chained job whose predecessor failed, under a dependency the
#: queue will now never satisfy. SLURM and PBS leave it sitting in the queue
#: looking PENDING for ever, which reads as "any minute now" and is the exact
#: opposite of the truth.
STATE_BLOCKED = "BLOCKED"

ALL_STATES = (
    STATE_NEW,
    STATE_UPLOADING,
    STATE_SUBMITTED,
    STATE_PENDING,
    STATE_RUNNING,
    STATE_COMPLETING,
    STATE_DOWNLOADING,
    STATE_DONE,
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_LOST,
)

#: States for which the poller still needs to contact the host.
ACTIVE_STATES = frozenset(
    {
        STATE_SUBMITTED,
        STATE_PENDING,
        STATE_RUNNING,
        STATE_COMPLETING,
    }
)

#: States that will never change again on their own.
TERMINAL_STATES = frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED, STATE_LOST})

BACKEND_OPENSSH = "openssh"
BACKEND_PARAMIKO = "paramiko"
#: This machine, no SSH at all.
BACKEND_LOCAL = "local"

SCHEDULER_SLURM = "slurm"
SCHEDULER_PBS = "pbs"
SCHEDULER_SGE = "sge"
SCHEDULER_SHELL = "shell"

#: Written by the generated run script; see schedulers.base.
SENTINEL_NAME = ".moleditpy_rc"
#: Touched by a chained job once its predecessor has finished and it starts
#: for real. Only chained jobs write it.
STARTED_NAME = ".moleditpy_started"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def new_id() -> str:
    """Short opaque identifier for a host / preset / job record."""
    return uuid.uuid4().hex[:12]


def sanitize_name(name: str, fallback: str = "job") -> str:
    """Reduce a user-supplied name to characters safe in a remote path."""
    cleaned = _SAFE_NAME_RE.sub("_", (name or "").strip()).strip("._-")
    return cleaned or fallback


def _from_dict(cls: type, data: Dict[str, Any]) -> Any:
    """Build a dataclass from a dict, ignoring unknown keys.

    Forward compatibility: a jobs.json written by a newer version of the plugin
    must not crash an older one.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class HostProfile:
    """Everything needed to reach one remote machine.

    No secret is ever stored here. Passwords for the paramiko backend live in
    memory for the session only.
    """

    id: str = field(default_factory=new_id)
    name: str = "cluster"
    hostname: str = ""
    username: str = ""
    port: int = 22
    backend: str = BACKEND_OPENSSH
    scheduler: str = SCHEDULER_SLURM
    key_path: str = ""
    jump_host: str = ""
    remote_root: str = "~/moleditpy_jobs"
    #: Run at most this many jobs at a time here; 0 means no limit. Matters
    #: most with no queue, where nothing else stops submissions piling onto the
    #: same cores. Enforced by chaining, so it holds with MoleditPy closed.
    max_concurrent: int = 0
    ssh_options: List[str] = field(default_factory=list)
    #: paramiko backend only: prompt for a password (never stored on disk).
    ask_password: bool = False
    connect_timeout: int = 10
    command_timeout: int = 60
    #: Prepended to every remote command (e.g. "source /etc/profile").
    login_commands: List[str] = field(default_factory=list)

    @property
    def is_local(self) -> bool:
        return self.backend == BACKEND_LOCAL

    @property
    def target(self) -> str:
        if self.is_local:
            return "this machine"
        return f"{self.username}@{self.hostname}" if self.username else self.hostname

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HostProfile":
        return _from_dict(cls, data)


@dataclass
class SubmitPreset:
    """Reusable resource request + command template for one host."""

    id: str = field(default_factory=new_id)
    host_id: str = ""
    name: str = "default"
    queue: str = ""
    account: str = ""
    walltime: str = "24:00:00"
    nodes: int = 1
    ntasks: int = 1
    cpus_per_task: int = 1
    memory: str = ""
    modules: List[str] = field(default_factory=list)
    pre_commands: List[str] = field(default_factory=list)
    #: {input} / {basename} / {stem} are substituted at submit time.
    command_template: str = "orca {input} > {stem}.out"
    fetch_globs: List[str] = field(
        default_factory=lambda: ["*.out", "*.log", "*.xyz", "*.hess", "*.fchk"]
    )
    auto_download: bool = True
    extra_directives: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubmitPreset":
        return _from_dict(cls, data)


@dataclass
class Job:
    """One tracked calculation."""

    id: str = field(default_factory=new_id)
    name: str = ""
    host_id: str = ""
    host_name: str = ""
    scheduler: str = SCHEDULER_SLURM
    remote_dir: str = ""
    remote_job_id: str = ""
    state: str = STATE_NEW
    rc: Optional[int] = None
    submitted_at: float = 0.0
    started_at: float = 0.0
    updated_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    local_dir: str = ""
    input_files: List[str] = field(default_factory=list)
    fetch_globs: List[str] = field(default_factory=list)
    auto_download: bool = True
    downloaded: bool = False
    downloaded_files: List[str] = field(default_factory=list)
    #: Relative to remote_dir; what "Tail Log" reads and what the round trip opens.
    log_file: str = ""
    command: str = ""
    #: The SubmitPreset used, snapshotted so Resubmit can reproduce the job
    #: even after the named preset is edited or deleted.
    preset: Dict[str, Any] = field(default_factory=dict)
    #: Id of the job this one was chained behind, on the same host. Its
    #: wrapper waits for that job's process before running anything.
    after_job_id: str = ""
    #: Chain on the predecessor *ending* rather than on it succeeding.
    chain_any: bool = False
    #: Epoch second before which the job must not start. 0 means "now".
    start_after: float = 0.0
    last_error: str = ""

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def elapsed(self, now: Optional[float] = None) -> float:
        """Seconds between submission and finish (or now, while running)."""
        if not self.submitted_at:
            return 0.0
        end = self.finished_at or (now if now is not None else time.time())
        return max(0.0, end - self.submitted_at)

    def touch(self, state: Optional[str] = None) -> None:
        if state is not None:
            self.state = state
            if state in TERMINAL_STATES and not self.finished_at:
                self.finished_at = time.time()
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        return _from_dict(cls, data)
