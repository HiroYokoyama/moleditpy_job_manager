"""Persistence for host profiles, submit presets and the job database.

Both files live in ``~/.moleditpy/job_manager/`` -- *outside* the plugin folder.
The Plugin Installer replaces the whole package directory on update and carries
over only a file literally named ``settings.json``, so anything kept beside the
code (the job list in particular) would be silently destroyed. A directory
under the user's MoleditPy home survives updates, reinstalls and
"Reset All Settings" alike.

* ``settings.json`` -- hosts, presets and preferences.
* ``jobs.pmejbs`` -- the tracked jobs. Global on purpose; HPC jobs outlive
  both the open project and the application session. Ordinary JSON inside; the
  extension is what makes it recognisable in a file dialog.

Both are written atomically (temp file in the same directory + ``os.replace``)
so a crash mid-write can never leave a truncated JSON behind.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    STATE_DONE,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_LOST,
    STATE_UPLOADING,
    TERMINAL_STATES,
    HostProfile,
    Job,
    SubmitPreset,
)

SETTINGS_FILENAME = "settings.json"

#: MoleditPy's own extension for a saved job list, alongside .pmeprj for
#: projects, and abbreviated the same way (pme + jbs).
#: The contents are ordinary JSON; the extension is what makes the file
#: recognisable, and what the open/save dialogs filter on.
JOB_EXTENSION = ".pmejbs"
JOBS_FILENAME = "jobs" + JOB_EXTENSION
#: Written by versions before the extension existed; read once, then migrated.
LEGACY_JOBS_FILENAME = "jobs.json"

#: Cleared job lists are kept here rather than deleted.
ARCHIVE_DIRNAME = "archived"
ARCHIVE_PREFIX = "jobs_"

#: Column order of a CSV export.
EXPORT_COLUMNS = (
    "id",
    "name",
    "host",
    "scheduler",
    "queue_id",
    "state",
    "exit_code",
    "submitted",
    "started",
    "finished",
    "elapsed_seconds",
    "remote_dir",
    "local_dir",
    "downloaded",
    "command",
    "last_error",
)

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
    #: Put a job's results in the directory its input came from, which is
    #: where the user is already working. The central download root is the
    #: fallback -- for a job with no local input to sit beside, and for
    #: anyone who would rather keep results out of their project folders.
    "download_beside_input": True,
    "auto_download": True,
    "download_all_outputs": True,
    "open_result_after_download": True,
    #: Off by default: the application icon belongs to the host, not to a
    #: plugin, and a badge is a change to how MoleditPy looks in the user's
    #: task bar. The status bar counter is always there and costs nobody
    #: anything, so this is opt-in.
    "taskbar_badge": False,
    #: On, unlike the badge: a notification is transient and describes an event
    #: the user asked to be told about by submitting a job that runs for hours.
    #: The badge is a persistent change to how MoleditPy looks; this is not.
    "notify_on_finish": True,
    "last_input_dir": "",
    #: Which file type the input picker opens on. Empty means the first one.
    "input_filter": "",
    #: The last submission's settings, per host id: walltime, queue, modules,
    #: command, fetch patterns and the rest. Everything in it describes the
    #: site rather than the molecule, so it is worth restoring; what the input
    #: file decides is not kept. {host_id: preset dict}.
    "last_preset": {},
    #: Read the core count and memory request out of the input file. On,
    #: because those numbers are already written there and the copy the queue
    #: schedules on is the one that gets forgotten.
    "scan_resources": True,
    #: The user's own command templates: [{"label": ..., "command": ...}].
    "command_templates": [],
    #: The command to use for an input extension, when the user has said which
    #: one they mean: {".inp": {"command": ..., "fetch_globs": [...]}}. This is
    #: how .inp stops being ambiguous -- ORCA, CP2K and GAMESS all write it, so
    #: the wizard will not guess, but it will remember an answer.
    "default_commands": {},
    #: Seconds between host-monitor samples, once the user has chosen. 0 means
    #: "not chosen", and the per-backend default applies.
    "host_monitor_interval": 0,
}


#: Overridable so tests never touch the real user directory.
DATA_DIR_ENV = "MOLEDITPY_JOB_MANAGER_DIR"


def _stamp(value: float) -> str:
    """A local ISO timestamp, or empty for "never happened"."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(value)) if value else ""


#: States a worker thread owns while it is uploading or fetching. Nothing can
#: still be in one after a restart, because the thread that would have moved it
#: on died with the process.
INTERRUPTED_STATES = frozenset({STATE_UPLOADING, STATE_DOWNLOADING})


def resolve_interrupted(job: Job) -> bool:
    """Give a job stranded mid-transfer an honest final state. True if changed.

    Neither UPLOADING nor DOWNLOADING is active or terminal, so a job left in
    one was invisible for good: the poller never looked at it again and prune
    never aged it out. A download only ever starts once the queue is finished,
    so the recorded exit code is the real outcome; an upload that never
    returned never reached the queue at all.
    """
    if job.state == STATE_UPLOADING:
        job.last_error = job.last_error or "Interrupted: MoleditPy closed during submission"
        job.touch(STATE_FAILED)
        return True
    if job.state == STATE_DOWNLOADING:
        if job.rc is None:
            job.touch(STATE_LOST)
        else:
            job.touch(STATE_DONE if job.rc == 0 else STATE_FAILED)
        return True
    return False


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
        self.default_jobs_path = os.path.join(self.directory, JOBS_FILENAME)
        self.jobs_path = self.default_jobs_path
        self.legacy_jobs_path = os.path.join(self.directory, LEGACY_JOBS_FILENAME)
        self.hosts: Dict[str, HostProfile] = {}
        self.presets: Dict[str, SubmitPreset] = {}
        self.jobs: Dict[str, Job] = {}
        #: True while the list in use was rebuilt from a folder rather than
        #: submitted from here. See :meth:`_document`.
        self.reconstructed = False
        self.prefs: Dict[str, Any] = dict(DEFAULT_PREFS)
        #: Ids removed on purpose in this session. Saving keeps jobs another
        #: instance wrote, and without this a removal would be undone by the
        #: very next save that read them back off disk.
        self._forgotten: set = set()
        #: Memo for :meth:`blocked_ids`, keyed by the chain state it was
        #: computed from.
        self._revision = 0
        self._blocked_key = -1
        self._blocked: frozenset = frozenset()
        #: What was last written to each file, so an unchanged document is not
        #: written again. See :meth:`_write_if_changed`.
        self._written: Dict[str, tuple] = {}
        self.load()

    # --- loading / saving ---------------------------------------------------

    def load(self) -> None:
        settings = read_json(self.settings_path, {}) or {}
        if not isinstance(settings, dict):
            settings = {}
        self.prefs = dict(DEFAULT_PREFS)
        self.prefs.update(settings.get("prefs") or {})
        self.hosts = {}
        for raw in settings.get("hosts") or []:
            if not isinstance(raw, dict):
                continue
            host = HostProfile.from_dict(raw)
            self.hosts[host.id] = host
        self.presets = {}
        for raw in settings.get("presets") or []:
            if not isinstance(raw, dict):
                continue
            preset = SubmitPreset.from_dict(raw)
            self.presets[preset.id] = preset

        source = self.jobs_path
        if not os.path.exists(source) and os.path.exists(self.legacy_jobs_path):
            # Written before .pmejbs existed. Read it here; the next save writes
            # the new name, and the old file is left alone as a fallback copy.
            source = self.legacy_jobs_path
        jobs_doc = read_json(source, {}) or {}
        if not isinstance(jobs_doc, dict):
            jobs_doc = {}
        self.jobs = {}
        for raw in jobs_doc.get("jobs") or []:
            if not isinstance(raw, dict):
                continue
            job = Job.from_dict(raw)
            self.jobs[job.id] = job
        self.invalidate_chains()
        self._resolve_interrupted()

    def _resolve_interrupted(self) -> int:
        """Settle any job left mid-transfer by a previous session."""
        stranded = [job for job in self.jobs.values() if resolve_interrupted(job)]
        for job in stranded:
            logging.info(
                "Job Manager: %s was interrupted mid-transfer; recorded as %s", job.name, job.state
            )
        return len(stranded)

    @staticmethod
    def _stat_key(path: str):
        """Enough of a file's identity to notice somebody else rewriting it."""
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _write_if_changed(self, path: str, document: Dict[str, Any], slot: str) -> bool:
        """Write ``document`` only when it differs from what is already there.

        Every write here is a temp file, an ``fsync`` and a rename. That is the
        right way to write a file that must never be found half-written, and
        the wrong thing to do on every keystroke in a text field or every step
        of a spin box -- which is what a preference saved on ``textChanged``
        amounts to. Serialising and comparing costs microseconds and turns a
        burst of identical saves into one.

        The file's own timestamp and size are remembered alongside, so a file
        that changed underneath us -- another window, or one truncated by
        something else -- is rewritten rather than assumed to still hold what
        we last put there.
        """
        serialised = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True)
        previous = self._written.get(slot)
        if previous is not None and previous == (serialised, self._stat_key(path)):
            return False
        atomic_write_json(path, document)
        self._written[slot] = (serialised, self._stat_key(path))
        return True

    def save_settings(self) -> None:
        self._write_if_changed(
            self.settings_path,
            {
                "version": 1,
                "prefs": self.prefs,
                "hosts": [h.to_dict() for h in self.hosts.values()],
                "presets": [p.to_dict() for p in self.presets.values()],
            },
            "settings",
        )

    def save_jobs(self) -> None:
        self.invalidate_chains()
        document = self._document(archived=False)
        document["jobs"] = self._merged_jobs(document["jobs"])
        self._write_if_changed(self.jobs_path, document, "jobs:" + self.jobs_path)

    def _merged_jobs(self, mine: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ours, plus any job on disk that this session has never heard of.

        Each write is atomic, but a read-modify-write across two processes is
        not: two MoleditPy windows each hold the whole list in memory, so the
        second to save wrote its own view straight over the first's, and the
        first's jobs were simply gone -- along with the remote directory that
        is often the only way back to results still sitting on the cluster.

        Ours win wherever both know a job, so nothing here can overwrite a
        state this session just observed. Jobs deliberately removed stay
        removed; that is what ``_forgotten`` is for.
        """
        disk = read_json(self.jobs_path, {}) or {}
        if not isinstance(disk, dict):
            disk = {}
        known = {raw.get("id") for raw in mine if isinstance(raw, dict)}
        extra = [
            raw
            for raw in (disk.get("jobs") or [])
            if isinstance(raw, dict)
            and raw.get("id")
            and raw["id"] not in known
            and raw["id"] not in self._forgotten
        ]
        return mine + extra if extra else mine

    def _document(self, archived: bool, when: Optional[float] = None) -> Dict[str, Any]:
        """One job-list file.

        ``archived`` is what makes a list history rather than working data, and
        it travels *in the file* rather than being inferred from where the file
        sits -- a list stays archived after it is moved, copied or mailed on.
        ``reconstructed`` travels the same way and for the same reason: a list
        built by reading a folder describes calculations nobody here submitted,
        so nothing in it may be cancelled, polled or resubmitted -- and that has
        to stay true after the file is closed, moved or opened somewhere else.
        """
        document: Dict[str, Any] = {
            "version": 1,
            "archived": bool(archived),
            "reconstructed": bool(self.reconstructed),
            "jobs": [j.to_dict() for j in self.job_list()],
        }
        if archived:
            document["archived_at"] = _stamp(when or time.time())
        return document

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

    def host_for_local_path(self, path: str) -> Optional[HostProfile]:
        """The enabled host whose local mirror holds ``path``, if any.

        The most specific wins: two hosts mirroring ``/mnt`` and ``/mnt/hpc``
        are both right about a file under the second, and the second is the
        useful answer.
        """
        matches = [
            host
            for host in self.host_list()
            if getattr(host, "enabled", True) and host.owns_local_path(path)
        ]
        if not matches:
            return None
        return max(
            matches, key=lambda host: len(os.path.abspath(os.path.expanduser(host.equal_path)))
        )

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
        self.invalidate_chains()
        self.save_jobs()
        return job

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        self.invalidate_chains()
        self._forgotten.add(job_id)
        self.save_jobs()

    def job_list(self) -> List[Job]:
        return sorted(
            self.jobs.values(), key=lambda j: j.submitted_at or j.updated_at, reverse=True
        )

    def active_jobs(self) -> List[Job]:
        return [j for j in self.jobs.values() if j.is_active]

    def chain_tail(self, host_id: str) -> Optional[Job]:
        """The job a new one should queue behind on this host, if any.

        The tail of the chain, not whatever happens to be running: each job
        waits for its predecessor, so appending to the newest active job is
        what makes a third, fourth and fifth submission line up behind the
        others instead of all starting at once.
        """
        candidates = [job for job in self.jobs.values() if job.host_id == host_id and job.is_active]
        # A job that will never start is not something to queue behind: joining
        # a chain that is already stranded strands the new job with it.
        runnable = [job for job in candidates if self.chain_blocker(job) is None]
        if not runnable:
            return None
        return max(runnable, key=lambda job: (job.submitted_at or job.updated_at))

    def runnable_jobs(self, host_id: str) -> List[Job]:
        """Active jobs on this host that are still going to run."""
        return [
            job
            for job in self.jobs.values()
            if job.host_id == host_id and job.is_active and self.chain_blocker(job) is None
        ]

    def chain_lanes(self, host_id: str) -> List[List[Job]]:
        """The chains currently in flight on this host, oldest job first.

        A "lane" is one dependency chain. With no queue to serialise anything,
        the number of lanes *is* the number of calculations running at once, so
        this is what a slot limit counts.
        """
        active = self.runnable_jobs(host_id)
        by_id = {job.id: job for job in active}
        # A job with an active successor is not the end of its chain.
        followed = {job.after_job_id for job in active if job.after_job_id in by_id}
        lanes: List[List[Job]] = []
        for tail in active:
            if tail.id in followed:
                continue
            chain = [tail]
            cursor = tail
            # Stop at a job already in this chain. Nothing the plugin writes
            # can point backwards -- a predecessor always exists before the job
            # that names it -- but a job list is a file, and this one is opened
            # by drag and drop from anywhere. A chain running into a cycle
            # walked it for ever, on the GUI thread, which is a frozen
            # application rather than a bad reading of a corrupt file.
            seen = {tail.id}
            while cursor.after_job_id in by_id and cursor.after_job_id not in seen:
                cursor = by_id[cursor.after_job_id]
                seen.add(cursor.id)
                chain.append(cursor)
            lanes.append(list(reversed(chain)))
        return lanes

    def free_slot(self, host_id: str, limit: int) -> bool:
        """True when a job submitted now would start straight away."""
        return limit <= 0 or len(self.chain_lanes(host_id)) < limit

    def chain_lane_tail(self, host_id: str, limit: int) -> Optional[Job]:
        """What a new job should queue behind to respect a slot limit.

        None means "start now": either there is no limit, or a lane is free.
        Otherwise the new job joins the *shortest* lane, which is what turns a
        limit of two and seven submissions into two balanced queues rather than
        one long chain and one job.

        Nothing here needs a daemon or a running MoleditPy: the waiting is the
        same dependency the scheduler (or the wrapper) already honours.
        """
        if limit <= 0:
            return None
        lanes = self.chain_lanes(host_id)
        if len(lanes) < limit:
            return None
        shortest = min(lanes, key=len)
        return shortest[-1]

    def chain_blocker(self, job: Job) -> Optional[Job]:
        """The dead job that will stop ``job`` ever starting, if any.

        Under an ``afterok`` dependency a predecessor that fails or is
        cancelled leaves everything behind it queued for ever: SLURM and PBS
        keep reporting PENDING, which reads as "starting soon" and is the
        opposite of the truth.

        The whole chain is walked, not just the job in front. Only the first
        job behind a failure used to count as blocked, so in A(failed) <- B <- C
        the plugin called B blocked and C merely queued -- and C is exactly as
        dead, since B will never start and so never end. That cost more than a
        wrong label: C counted as a live lane, and held one of the host's slots
        for the rest of the session.

        ``chain_any`` is read at the link that meets the failure, not at the
        job being asked about. A job chained behind one that *ended* badly is
        released; a job chained behind one that never starts is not, however
        loose its own dependency, because it never ends either.
        """
        from .schedulers import get_scheduler

        if not job.is_active:
            return None
        cursor = job
        # A job list is a file, and one can be opened by drag and drop from
        # anywhere; a chain running into a cycle would walk it for ever on the
        # GUI thread.
        seen = {job.id}
        while cursor.after_job_id:
            predecessor = self.jobs.get(cursor.after_job_id)
            if predecessor is None or predecessor.id in seen:
                return None
            if not predecessor.is_terminal:
                # Still going to run, unless something further back is dead.
                seen.add(predecessor.id)
                cursor = predecessor
                continue
            if predecessor.state == STATE_DONE or cursor.chain_any:
                return None
            try:
                scheduler = get_scheduler(cursor.scheduler)
            except ValueError:
                return None
            return None if scheduler.chain_releases_on_failure else predecessor
        return None

    def blocked_ids(self) -> frozenset:
        """Ids of every active job that will never start. Cached.

        :meth:`chain_blocker` walks a chain and asks the scheduler registry a
        question for each link, and the table asks it twice per visible row per
        repaint -- once for the text and once for the colour -- on top of the
        status bar counter and the host monitor doing the same for every job in
        the list. Answering once per actual change and handing out the set is
        the same answer for a fraction of the work.

        Invalidated by :meth:`invalidate_chains`, which every write goes
        through. A caller that changes a job's state without saving must say so
        -- but every path in the plugin that changes one saves it in the same
        breath, which is what makes a counter enough here rather than a
        signature over the whole list recomputed on each repaint.
        """
        if self._blocked_key != self._revision:
            self._blocked_key = self._revision
            self._blocked = frozenset(
                job.id for job in self.jobs.values() if self.chain_blocker(job) is not None
            )
        return self._blocked

    def invalidate_chains(self) -> None:
        """Drop the cached chain analysis; the next reader recomputes it."""
        self._revision += 1

    def dependents_of(self, job_id: str, recursive: bool = False) -> List[Job]:
        """Every job chained behind this one.

        Directly by default. ``recursive`` follows the chain to its end, which
        is what "everything this failure has stranded" means.
        """
        direct = [job for job in self.jobs.values() if job.after_job_id == job_id]
        if not recursive:
            return direct
        found: List[Job] = []
        seen = {job_id}
        queue = list(direct)
        while queue:
            job = queue.pop(0)
            if job.id in seen:
                continue
            seen.add(job.id)
            found.append(job)
            queue.extend(j for j in self.jobs.values() if j.after_job_id == job.id)
        return found

    def active_jobs_by_host(self) -> Dict[str, List[Job]]:
        grouped: Dict[str, List[Job]] = {}
        for job in self.active_jobs():
            grouped.setdefault(job.host_id, []).append(job)
        return grouped

    # --- archiving and export -----------------------------------------------

    def archive_dir(self) -> str:
        """Where cleared job lists are kept: ``<data dir>/archived``."""
        return os.path.join(self.directory, ARCHIVE_DIRNAME)

    def archive_jobs(self, when: Optional[float] = None) -> str:
        """Write the current list to ``old/jobs_<date>.json``; returns its path.

        Clearing the table must never be the same thing as losing the record:
        a job's remote directory is often the only way back to results that are
        still on the cluster.
        """
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when or time.time()))
        directory = self.archive_dir()
        target = os.path.join(directory, f"{ARCHIVE_PREFIX}{stamp}{JOB_EXTENSION}")
        suffix = 2
        while os.path.exists(target):
            target = os.path.join(directory, f"{ARCHIVE_PREFIX}{stamp}_{suffix}{JOB_EXTENSION}")
            suffix += 1
        atomic_write_json(target, self._document(archived=True, when=when))
        return target

    def clear_jobs(self, when: Optional[float] = None) -> Tuple[str, int]:
        """Archive the list, then empty it. Returns (archive path, count)."""
        count = len(self.jobs)
        archived = self.archive_jobs(when)
        self._forgotten.update(self.jobs)
        self.jobs = {}
        self.invalidate_chains()
        self.save_jobs()
        return archived, count

    def archived_files(self) -> List[str]:
        """Every archived list, newest first."""
        directory = self.archive_dir()
        if not os.path.isdir(directory):
            return []
        names = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            # .json too: archives written before the extension existed.
            if name.startswith(ARCHIVE_PREFIX) and name.endswith((JOB_EXTENSION, ".json"))
        ]
        return sorted(names, reverse=True)

    def read_job_list(self, path: str) -> Tuple[List[Job], bool]:
        """Read a job list file. Returns (jobs, archived).

        ``archived`` decides how the caller shows it: an archived job's queue id
        is stale and its remote directory may be long gone, so offering to
        cancel or resubmit from one would be offering something that cannot
        work. Anything else is working data and is merged into the live list.
        """
        payload = read_json(path, {}) or {}
        if not isinstance(payload, dict):
            return [], False
        jobs = [Job.from_dict(raw) for raw in payload.get("jobs") or [] if isinstance(raw, dict)]
        return jobs, bool(payload.get("archived", False))

    def write_job_list(self, path: str, jobs: List[Job], reconstructed: bool = False) -> str:
        """Write ``jobs`` to ``path`` as a job list that can be opened again."""
        atomic_write_json(
            path,
            {
                "version": 1,
                "archived": False,
                "reconstructed": bool(reconstructed),
                "jobs": [job.to_dict() for job in jobs],
            },
        )
        return path

    def read_job_flags(self, path: str) -> Dict[str, bool]:
        """The two flags a job list carries about itself."""
        payload = read_json(path, {}) or {}
        if not isinstance(payload, dict):
            return {"archived": False, "reconstructed": False}
        return {
            "archived": bool(payload.get("archived", False)),
            "reconstructed": bool(payload.get("reconstructed", False)),
        }

    def use_jobs_file(self, path: str) -> int:
        """Make ``path`` the live job list. Returns how many jobs it holds.

        Opening a list *switches* to it rather than merging: the file becomes
        the one every later change is written to. The choice lasts for the
        session only -- the next start comes back to the default list, so an
        afternoon spent looking at someone else's file cannot quietly become
        the permanent home of your own jobs. Pass an empty path to switch back.
        """
        target = os.path.abspath(os.path.expanduser(path)) if path else ""
        self.jobs_path = target or self.default_jobs_path
        # Read before the jobs, and never assumed: going back to the default
        # list has to clear the flag as surely as opening a rebuilt one sets it.
        self.reconstructed = (
            self.read_job_flags(self.jobs_path)["reconstructed"] if target else False
        )
        jobs, _archived = self.read_job_list(self.jobs_path)
        self.jobs = {job.id: job for job in jobs}
        self.invalidate_chains()
        # Removals applied to the list being left, not to this one: carrying
        # them over would silently drop a job from the file just opened.
        self._forgotten = set()
        self._resolve_interrupted()
        return len(self.jobs)

    def using_default_jobs_file(self) -> bool:
        return os.path.normcase(os.path.abspath(self.jobs_path)) == os.path.normcase(
            os.path.abspath(self.default_jobs_path)
        )

    def export_jobs(self, path: str) -> str:
        """Write the job list to ``path``: CSV by extension, else .pmejbs JSON."""
        if os.path.splitext(path)[1].lower() == ".csv":
            self.export_jobs_csv(path)
        else:
            self.export_jobs_json(path)
        return path

    def export_jobs_json(self, path: str) -> None:
        """The raw records, exactly as the live list holds them.

        Not flagged archived: an export is a copy of working data, and opening
        one puts its jobs back under tracking.
        """
        atomic_write_json(path, self._document(archived=False))

    def export_jobs_csv(self, path: str) -> None:
        """One row per job, timestamps rendered as local ISO strings."""
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_COLUMNS)
            for job in self.job_list():
                writer.writerow(
                    [
                        job.id,
                        job.name,
                        job.host_name,
                        job.scheduler,
                        job.remote_job_id,
                        job.state,
                        "" if job.rc is None else job.rc,
                        _stamp(job.submitted_at),
                        _stamp(job.started_at),
                        _stamp(job.finished_at),
                        round(job.elapsed(), 1),
                        job.remote_dir,
                        job.local_dir,
                        "yes" if job.downloaded else "no",
                        (job.command or "").replace("\n", " ").strip(),
                        (job.last_error or "").replace("\n", " ").strip(),
                    ]
                )

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
            self._forgotten.add(job_id)
        if stale:
            self.save_jobs()
        return len(stale)

    # --- prefs --------------------------------------------------------------

    def get_pref(self, key: str, default: Any = None) -> Any:
        return self.prefs.get(key, DEFAULT_PREFS.get(key, default))

    def set_pref(self, key: str, value: Any) -> None:
        """Remember a preference. Writing is skipped when nothing changed."""
        if key in self.prefs and self.prefs[key] == value:
            return
        self.prefs[key] = value
        self.save_settings()

    # --- user command templates ---------------------------------------------

    def user_templates(self) -> List[Dict[str, Any]]:
        """The user's own command templates, preserving all stored fields.

        Returns dicts with at least ``"label"`` and ``"command"``; additional
        fields such as ``"fetch_globs"`` are passed through unchanged so that
        :meth:`add_user_template` can re-save them without losing data.
        """
        raw = self.get_pref("command_templates", []) or []
        result = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("label"):
                continue
            entry: Dict[str, Any] = {
                "label": str(item.get("label", "")),
                "command": str(item.get("command", "")),
            }
            if item.get("fetch_globs"):
                entry["fetch_globs"] = list(item["fetch_globs"])
            result.append(entry)
        return result

    def default_command_for(self, extension: str) -> Dict[str, Any]:
        """What this user runs for that input extension, or an empty dict."""
        stored = self.get_pref("default_commands", {}) or {}
        return dict(stored.get((extension or "").lower(), {}))

    def set_default_command(
        self, extension: str, command: str, fetch_globs: Optional[List[str]] = None
    ) -> None:
        """Remember a command for an extension; empty command forgets it."""
        extension = (extension or "").lower()
        if not extension:
            return
        stored = dict(self.get_pref("default_commands", {}) or {})
        if not (command or "").strip():
            stored.pop(extension, None)
        else:
            entry: Dict[str, Any] = {"command": command}
            if fetch_globs:
                entry["fetch_globs"] = list(fetch_globs)
            stored[extension] = entry
        self.set_pref("default_commands", stored)

    def add_user_template(
        self, label: str, command: str, fetch_globs: Optional[List[str]] = None
    ) -> None:
        """Save (or replace) one template. Persisted in settings.json.

        The fetch patterns travel with the command: they describe the same
        program, and a saved template that brings back the wrong files is not
        much of a saving.
        """
        label = (label or "").strip()
        if not label:
            return
        templates = [t for t in self.user_templates() if t["label"] != label]
        entry = {"label": label, "command": command or ""}
        if fetch_globs:
            entry["fetch_globs"] = list(fetch_globs)
        templates.append(entry)
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
