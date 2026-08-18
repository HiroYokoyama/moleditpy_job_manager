"""Rebuild a job list from a folder of results that were never tracked.

The case this exists for: the calculations are already on disk -- fetched by
hand, copied off a cluster, produced before this plugin was installed, or left
behind by a job list that was cleared -- and there is no record of them here at
all. Everything the monitor can do with a finished job (open the result, read
what is beside it, export the lot) needs a job record, so this makes one per
directory that looks like a calculation.

Nothing is contacted and nothing is written into the folder: it is read, and
the records are handed back. Pure Python, no Qt, so a walk of a slow network
share can run on a worker thread.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Sequence

from .models import SENTINEL_NAME, STATE_DONE, STATE_FAILED, Job

#: Extensions that make a directory worth a job record. Deliberately the
#: outputs, not everything: a folder of inputs has nothing to open yet.
RESULT_EXTENSIONS = (
    ".out",
    ".log",
    ".fchk",
    ".hess",
    ".molden",
    ".cube",
    ".chk",
    ".gbw",
    ".wfn",
    ".wfx",
    ".engrad",
    ".xyz",
)

#: Inputs, so a reconstructed job can be resubmitted and knows its own name.
INPUT_EXTENSIONS = (".inp", ".com", ".gjf", ".in", ".nw", ".gjc")

#: Never walked into: none of them holds a calculation, and one of them holds
#: tens of thousands of files.
SKIP_DIRECTORIES = frozenset(
    {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}
)

#: How far below the chosen folder to look. A project tree is a few levels
#: deep; anything past this is somebody having picked their home directory.
MAX_DEPTH = 6

#: Ceiling on the walk, so picking the wrong folder costs a moment rather than
#: a frozen window. Reaching it is reported, never silently truncated.
MAX_FILES = 20000


class ScanResult:
    """What one folder scan found."""

    def __init__(self, jobs: List[Job], files_seen: int, truncated: bool) -> None:
        self.jobs = jobs
        self.files_seen = files_seen
        self.truncated = truncated

    def __len__(self) -> int:
        return len(self.jobs)


def _is_result(name: str) -> bool:
    return name.lower().endswith(RESULT_EXTENSIONS)


def _is_input(name: str) -> bool:
    return name.lower().endswith(INPUT_EXTENSIONS)


def _read_sentinel(directory: str, names: Sequence[str]) -> Optional[int]:
    """The exit code this plugin's own wrapper left behind, if it is there.

    A directory that a job of ours ran in carries its real outcome, so a
    rebuilt record can say FAILED where it failed instead of calling everything
    it finds a success.

    Read from the listing the walk already has: a second listdir per directory
    is a second pass over a tree that may be on a network share.
    """
    for name in names:
        if not name.startswith(SENTINEL_NAME):
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
                return int((handle.read() or "").strip().splitlines()[0])
        except (OSError, ValueError, IndexError):
            return None
    return None


def _job_for(directory: str, names: Sequence[str], root: str) -> Optional[Job]:
    """One job record for one directory, or None if it holds no results."""
    results = sorted(name for name in names if _is_result(name) and not name.startswith("."))
    if not results:
        return None
    inputs = sorted(name for name in names if _is_input(name) and not name.startswith("."))
    paths = [os.path.join(directory, name) for name in results]

    newest = 0.0
    oldest = 0.0
    for path in paths:
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            continue
        newest = max(newest, stamp)
        oldest = min(oldest, stamp) if oldest else stamp

    # The directory name, unless the folder chosen *is* the directory -- then
    # the longest-lived output names it, which is what the file is called.
    label = os.path.basename(directory.rstrip(os.sep)) or os.path.basename(root.rstrip(os.sep))
    if inputs:
        label = os.path.splitext(inputs[0])[0]

    rc = _read_sentinel(directory, names)
    job = Job(
        name=label or "job",
        state=STATE_DONE if rc in (0, None) else STATE_FAILED,
        rc=rc,
        submitted_at=oldest or newest,
        started_at=oldest or newest,
        finished_at=newest,
        updated_at=newest or time.time(),
        local_dir=directory,
        downloaded=True,
        downloaded_files=paths,
        input_files=[os.path.join(directory, name) for name in inputs],
        last_error="" if rc in (0, None) else f"the job directory records exit code {rc}",
    )
    return job


def scan_folder(root: str, max_depth: int = MAX_DEPTH, max_files: int = MAX_FILES) -> ScanResult:
    """Walk ``root`` and build one job record per directory of results.

    Ordered newest first, like the live list, so the calculation finished last
    is at the top where it is looked for.
    """
    root = os.path.abspath(os.path.expanduser(root or ""))
    if not os.path.isdir(root):
        return ScanResult([], 0, False)

    jobs: List[Job] = []
    seen = 0
    truncated = False
    root_depth = root.rstrip(os.sep).count(os.sep)
    for directory, subdirectories, names in os.walk(root):
        # Pruned in place, which is what stops os.walk descending into them.
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in SKIP_DIRECTORIES and not name.startswith(".")
        )
        if directory.rstrip(os.sep).count(os.sep) - root_depth >= max_depth:
            subdirectories[:] = []
        seen += len(names)
        if seen > max_files:
            truncated = True
            subdirectories[:] = []
            continue
        try:
            job = _job_for(directory, names, root)
        except OSError:
            logging.debug("Job Manager: could not read %s", directory, exc_info=True)
            continue
        if job is not None:
            jobs.append(job)

    jobs.sort(key=lambda job: job.finished_at or job.updated_at, reverse=True)
    return ScanResult(jobs, seen, truncated)


def summarise(result: ScanResult) -> Dict[str, int]:
    """Counts for the message shown after a scan."""
    return {
        "jobs": len(result.jobs),
        "files": sum(len(job.downloaded_files) for job in result.jobs),
        "failed": sum(1 for job in result.jobs if job.state == STATE_FAILED),
    }


__all__ = [
    "INPUT_EXTENSIONS",
    "MAX_DEPTH",
    "MAX_FILES",
    "RESULT_EXTENSIONS",
    "ScanResult",
    "scan_folder",
    "summarise",
]
