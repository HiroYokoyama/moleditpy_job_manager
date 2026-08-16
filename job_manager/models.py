"""Data model for hosts, submit presets and tracked jobs.

Pure Python: no Qt, no RDKit, no network. Everything here must stay importable
with nothing but the standard library so the headless test suite (and CI, which
installs only pytest) can exercise it directly.
"""

from __future__ import annotations

import os
import posixpath
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
#: Native Windows: PowerShell wrapper, no POSIX shell needed.
SCHEDULER_WINDOWS = "windows"

#: Read before every command on a host with ``load_profile`` set, in the order a
#: login shell reads them. Each is tested first so a missing file is not an
#: error, and each is allowed to fail: a dotfile that ends non-zero (a `module`
#: that warns, an `stty` on a shell with no terminal) must not become the
#: job's exit code.
#:
#: Debian's stock ~/.bashrc returns immediately for a non-interactive shell, so
#: this is not a complete substitute for a login shell -- but anything the user
#: put *above* that guard, which is where module loads usually end up, does run.
PROFILE_COMMANDS = (
    # First, or the aliases the next four lines define are read as ordinary
    # words: bash expands aliases only in an interactive shell unless told
    # otherwise, and a job script is not one. A user whose launch command is
    # `alias myorca=...` in ~/.bashrc gets "myorca: command not found" without it.
    "shopt -s expand_aliases 2>/dev/null || true",
    "[ -f /etc/profile ] && . /etc/profile || true",
    "[ -f ~/.bash_profile ] && . ~/.bash_profile || true",
    "[ -f ~/.profile ] && . ~/.profile || true",
    "[ -f ~/.bashrc ] && . ~/.bashrc || true",
)

#: How a host keeps its concurrency limit. See ``HostProfile.concurrency_mode``.
MODE_LANES = "lanes"
MODE_RUNNER = "runner"

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
    """Build a dataclass from a dict, ignoring unknown or malformed values.

    Forward compatibility: a jobs.json written by a newer version of the plugin
    must not crash an older one. A damaged record is treated like an empty one;
    the store decides whether to retain or skip that record.
    """
    if not isinstance(data, dict):
        data = {}
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


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
    #: The built-in mode, which needs nothing installed on the far end. A host
    #: with a real queue is a deliberate choice; guessing SLURM for every new
    #: profile was not.
    scheduler: str = SCHEDULER_SHELL
    key_path: str = ""
    jump_host: str = ""
    remote_root: str = "~/moleditpy_jobs"
    #: Run at most this many jobs at a time here; 0 means no limit. Matters
    #: most with no queue, where nothing else stops submissions piling onto the
    #: same cores.
    max_concurrent: int = 0
    #: How that limit is kept. ``lanes`` chains submissions together and leaves
    #: nothing on the host; ``runner`` puts a small queue there instead, which
    #: can reorder, count cores and memory, and free a slot the moment a job
    #: ends.
    #:
    #: The runner is the default because it is the only one of the two that can
    #: schedule on resources at all: chained lanes fix the order at submit time
    #: and know nothing about cores or memory, so a host left on lanes ignores
    #: every limit set for it bar the job count. It only ever applies where
    #: there is no scheduler already (see :attr:`uses_remote_runner`), and it
    #: exits by itself the moment its queue is empty.
    concurrency_mode: str = MODE_RUNNER
    #: Cores the remote runner may hand out. 0 asks the machine (``nproc``),
    #: which only happens when :attr:`runner_detect` is set.
    runner_cores: int = 0
    #: Megabytes of memory the remote runner may hand out. 0 asks the machine.
    #: A second budget beside the cores, because two jobs of 90 GB on a 120 GB
    #: machine must not both start just because the cores happened to be free.
    runner_memory_mb: int = 0
    #: Let the helper read the machine's own cores and memory instead of using
    #: the two numbers above. Off for a new profile: what a shared login node
    #: reports is the whole machine, not the share you are entitled to, so the
    #: honest default is the number the user actually knows.
    runner_detect: bool = False
    ssh_options: List[str] = field(default_factory=list)
    #: paramiko backend only: prompt for a password (never stored on disk).
    ask_password: bool = False
    connect_timeout: int = 10
    command_timeout: int = 60
    #: Prepended to every remote command (e.g. "source /etc/profile").
    login_commands: List[str] = field(default_factory=list)
    #: Read the login files before anything else runs. ``ssh host 'cmd'`` gets a
    #: shell that is neither login nor interactive, so none of /etc/profile,
    #: ~/.bash_profile or ~/.bashrc is read -- and those are exactly where a
    #: module system, a conda hook or a hand-installed program puts its PATH.
    #: Without this, a program that runs when you ssh in by hand is simply not
    #: found by the job, which is the single most common way a submission fails.
    load_profile: bool = True
    #: Off means the host is skipped by the monitor, submit wizard and the Host
    #: Monitor's live panel -- kept in the list rather than deleted, for a
    #: machine that is down for maintenance or a account you are between uses of.
    enabled: bool = True
    #: A local path that mirrors this host's filesystem (a Samba share, a
    #: mapped drive, an sshfs mount) -- if set, ``equal_path`` + a job's
    #: remote-relative path *is* the file, with nothing to download. Empty
    #: means there is no such mirror and results are fetched over the
    #: transport as before.
    equal_path: str = ""

    @property
    def is_local(self) -> bool:
        return self.backend == BACKEND_LOCAL

    def environment_commands(self) -> List[str]:
        """The login files to read, then whatever the user added.

        Order matters and follows what a login shell does: the system file, then
        the per-user login file, then the interactive one. Each is guarded, so a
        host missing any of them is not an error, and ``|| true`` keeps a
        dotfile that exits non-zero from taking the job down with it.

        Nothing is emitted for a Windows host: its commands are PowerShell, and
        it is driven with -NoProfile deliberately.
        """
        if not self.load_profile or self.scheduler == SCHEDULER_WINDOWS:
            return list(self.login_commands or [])
        return list(PROFILE_COMMANDS) + list(self.login_commands or [])

    @property
    def uses_remote_runner(self) -> bool:
        """A queue on the host only makes sense where there is not one already.

        Both no-queue schedulers qualify -- bash and Windows each have their
        own runner. A real cluster does not: it already has a scheduler, and a
        second one on the login node is what sysadmins object to.
        """
        return self.concurrency_mode == MODE_RUNNER and self.scheduler in (
            SCHEDULER_SHELL,
            SCHEDULER_WINDOWS,
        )

    @property
    def target(self) -> str:
        if self.is_local:
            return "this machine"
        return f"{self.username}@{self.hostname}" if self.username else self.hostname

    def mirrored_path(self, relative_path: str) -> str:
        """The local path ``relative_path`` (posix-separated, under the job's
        remote directory) maps to under :attr:`equal_path`, or "" if this host
        has no mirror configured."""
        if not self.equal_path or not relative_path:
            return ""
        parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
        expanded_root = os.path.expanduser(self.equal_path)
        mirror_root = expanded_root if os.path.isabs(expanded_root) else os.path.abspath(expanded_root)
        return os.path.join(mirror_root, *parts) if parts else mirror_root

    def mirrored_job_dir(self, remote_dir: str) -> str:
        """Map a remote job directory below ``remote_root`` to its mirror."""
        if not self.equal_path or not remote_dir or not self.remote_root:
            return ""
        remote = str(remote_dir).replace("\\", "/").rstrip("/") or "/"
        root = str(self.remote_root).replace("\\", "/").rstrip("/") or "/"
        remote_norm = posixpath.normpath(remote)
        root_norm = posixpath.normpath(root)
        if remote_norm == root_norm:
            relative = ""
        elif remote_norm.startswith(root_norm.rstrip("/") + "/"):
            relative = remote_norm[len(root_norm.rstrip("/")) :].lstrip("/")
        else:
            return ""
        return self.mirrored_path(relative)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HostProfile":
        host = _from_dict(cls, data)
        if "concurrency_mode" not in (data or {}):
            # Saved before the helper queue existed, so this host has been
            # chaining lanes all along. A new profile defaults to the runner,
            # but an upgrade must not quietly move someone's jobs onto a
            # different scheduler while they are not looking.
            host.concurrency_mode = MODE_LANES
        if "runner_detect" not in (data or {}):
            # Saved when 0 meant "ask the machine". Keep that host detecting
            # rather than silently handing its queue a budget of nothing.
            host.runner_detect = not (host.runner_cores or host.runner_memory_mb)
        if "load_profile" not in (data or {}):
            # An existing host has been running without the login files read,
            # and whatever it runs today works that way. Reading them now could
            # change which build of a program a running batch resolves to, so
            # the new behaviour is for new profiles only.
            host.load_profile = False
        return host


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
    #: The job runs in a directory the *user* prepared, rather than in one this
    #: plugin made for it. Everything the wrapper writes is then named per job
    #: (see :attr:`script_name`), because that directory is shared: it holds the
    #: user's own files, and very likely other jobs submitted into it as well.
    remote_dir_provided: bool = False
    #: A file already on the host, standing in for the uploaded input in
    #: ``{input}`` / ``{stem}``. Relative to :attr:`remote_dir`.
    remote_input: str = ""
    #: Wrapper script name; empty means the scheduler's shared default.
    script_name: str = ""
    #: Completion sentinel name; empty means :data:`SENTINEL_NAME`.
    sentinel_name: str = ""
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
        """Wall-clock seconds the job has been *running* (since it actually started).

        If :attr:`started_at` is set, counts from ``started_at`` to
        ``finished_at`` (or ``now``). If ``started_at`` is not set but the job
        is running or finished, falls back to ``submitted_at``.
        """
        start = self.started_at
        if not start:
            if (
                self.finished_at
                or self.state in (STATE_RUNNING, STATE_COMPLETING, STATE_DOWNLOADING)
                or self.is_terminal
            ):
                start = self.submitted_at
            else:
                return 0.0
        if not start:
            return 0.0
        end = self.finished_at or (now if now is not None else time.time())
        return max(0.0, end - start)

    def waiting(self, now: Optional[float] = None) -> float:
        """Wall-clock seconds the job spent waiting in the queue before starting.

        While the job is pending/queued, counts from ``submitted_at`` to ``now``.
        Once the job starts running (or finishes), freezes at the queue duration.
        """
        if not self.submitted_at:
            return 0.0
        end = self.started_at or self.finished_at or (now if now is not None else time.time())
        return max(0.0, end - self.submitted_at)

    def touch(self, state: Optional[str] = None) -> None:
        if state is not None:
            self.state = state
            if state == STATE_RUNNING and not self.started_at:
                self.started_at = time.time()
            if state in TERMINAL_STATES and not self.finished_at:
                self.finished_at = time.time()
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        return _from_dict(cls, data)
