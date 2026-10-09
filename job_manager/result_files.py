"""Output discovery, pattern selection and contained atomic downloads."""

from __future__ import annotations

import fnmatch
import logging
import os
import tempfile
from typing import Iterable, List, Optional, Sequence

from . import dialect, remote_paths
from .models import (
    SENTINEL_NAME,
    Job,
)
from .transport.base import Transport, TransportError

# Downloads are renamed only after a complete transfer.
PARTIAL_SUFFIX = ".moleditpy-part"


#: Files this plugin puts in the job directory itself. None of them is a
#: result: the wrapper's log holds whatever the command wrote to the terminal,
#: the sentinel holds one number, and the script is the script. Tail Log reads
#: the log on the host, which is where it belongs.
def is_plugin_file(name: str, log_file: str = "") -> bool:
    """True for a file this plugin wrote, rather than the calculation."""
    from .models import STARTED_NAME

    leaf = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if log_file and (name == log_file or leaf == f"{log_file}.err"):
        return True
    return (
        leaf.startswith(SENTINEL_NAME)
        or leaf.startswith(STARTED_NAME)
        or leaf.startswith("moleditpy_run.")
        or (leaf.startswith("moleditpy_") and leaf.endswith(".log"))
    )


#: Extensions a calculation writes, best first. The order is what decides which
#: file is offered when several could be opened.
RESULT_PRIORITY = (".out", ".log", ".fchk", ".hess", ".molden", ".cube", ".xyz")


def likely_outputs(names: Sequence[str], log_file: str = "") -> List[str]:
    """The files worth offering, in priority order; never the plugin's own.

    Used to preselect: the Open Result window opens on one of these, and the
    download chooser ticks them when the fetch patterns matched nothing at all
    -- which is the case it exists for. ``job.log`` is excluded however the
    ranking falls, because it is this plugin's file: the outputs the user cares
    about are the ones the command was told to write.
    """
    candidates = [name for name in names or [] if name and not is_plugin_file(name, log_file)]
    ranked: List[str] = []
    for extension in RESULT_PRIORITY:
        ranked += [name for name in candidates if name.lower().endswith(extension)]
    # Deduplicated, keeping the best rank each file reached.
    seen = set()
    ordered = []
    for name in ranked:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def primary_output(names: Sequence[str], log_file: str = "") -> str:
    """The one file to open, or "" when none of them looks like a result."""
    ranked = likely_outputs(names, log_file)
    return ranked[0] if ranked else ""


#: How deep a fetch pattern may reach. A pattern is allowed to name a
#: sub-directory, but not to turn a download into a walk of a scratch tree.
MAX_FETCH_DEPTH = 4


def list_remote_files(transport: Transport, remote_dir: str, depth: int = 1) -> List[str]:
    """File names inside ``remote_dir``, ``depth`` levels down.

    Depth 1 is the plain listing and stays the default: recursion is a more
    expensive command, and it is only worth it when a fetch pattern names a
    sub-directory.

    Whatever the depth, every name is validated before it is returned. The
    listing comes from the remote machine and is later joined onto a local
    directory to download into, so a name like ``../../.bashrc`` would write
    outside the download folder.
    """
    speaker = dialect.for_host(transport.host)
    if depth > 1:
        result = transport.run(speaker.list_tree(remote_dir, depth))
    else:
        result = transport.run(speaker.list_dir(remote_dir))
    names = []
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if not name or name.endswith("/"):
            continue
        safe = safe_relative_name(name) if depth > 1 else safe_download_name(name)
        if name.replace("\\", "/") != safe:
            logging.warning("Job Manager: skipping suspicious remote file name %r", name)
            continue
        names.append(safe)
    return names


def safe_download_name(name: str) -> str:
    """The bare file name, or "" if ``name`` is not one."""
    cleaned = (name or "").replace("\\", "/").strip()
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("/"):
        return ""
    if "/" in cleaned or os.path.splitdrive(cleaned)[0]:
        return ""
    return cleaned


def safe_relative_name(name: str) -> str:
    """The same, but allowing sub-directories: ``scratch/mol.out``.

    Every name here comes from the remote machine and is then joined onto a
    local directory to write into, so the rules are the ones that keep it
    inside: nothing absolute, no drive letter, and no ``..`` in any segment.
    The result is always spelled with forward slashes.
    """
    cleaned = (name or "").replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or os.path.splitdrive(cleaned)[0]:
        return ""
    segments = [part for part in cleaned.split("/") if part not in ("", ".")]
    if not segments or any(part == ".." for part in segments):
        return ""
    return "/".join(segments)


def matches_pattern(name: str, pattern: str) -> bool:
    """fnmatch, but a ``*`` stops at a directory boundary.

    ``fnmatch`` alone treats the whole string as one word, so ``*.out`` would
    match ``scratch/mol.out`` the moment the listing went one level deep --
    quietly changing what every existing pattern means. Matching segment by
    segment keeps ``*.out`` meaning "in the job directory", and makes
    ``scratch/*.out`` and ``*/*.out`` say what they look like they say.

    ``**`` stands for any number of directories, as it does everywhere else.
    """
    name_parts = [p for p in (name or "").split("/") if p]
    pattern_parts = [p for p in (pattern or "").split("/") if p]

    def match_from(name_index: int, pattern_index: int) -> bool:
        while pattern_index < len(pattern_parts):
            part = pattern_parts[pattern_index]
            if part == "**":
                # Try consuming nothing, then one segment, then two...
                for skip in range(name_index, len(name_parts) + 1):
                    if match_from(skip, pattern_index + 1):
                        return True
                return False
            if name_index >= len(name_parts):
                return False
            if not fnmatch.fnmatch(name_parts[name_index], part):
                return False
            name_index += 1
            pattern_index += 1
        return name_index == len(name_parts)

    return match_from(0, 0)


def pattern_depth(globs: Sequence[str]) -> int:
    """How deep the listing has to go for these patterns. 1 is no recursion.

    Depth costs a recursive listing on the far end, so it is taken from what
    was actually asked for rather than applied always. ``**`` is capped: a
    pattern that means "anywhere" must not turn a fetch into a walk of a
    scratch directory with a hundred thousand files in it.
    """
    depth = 1
    for pattern in globs or []:
        parts = [p for p in (pattern or "").strip().split("/") if p]
        if any(part == "**" for part in parts):
            return MAX_FETCH_DEPTH
        depth = max(depth, len(parts))
    return min(depth, MAX_FETCH_DEPTH)


def select_files(names: Iterable[str], globs: Sequence[str]) -> List[str]:
    patterns = [g.strip() for g in (globs or []) if g and g.strip()]
    if not patterns:
        return list(names)
    selected = []
    for name in names:
        if any(matches_pattern(name, pattern) for pattern in patterns):
            selected.append(name)
    return selected


def fetch_results(
    transport: Transport, job: Job, local_dir: str, globs: Optional[Sequence[str]] = None
) -> List[str]:
    """Download everything in the job directory matching the fetch globs."""
    patterns = [p for p in (globs if globs is not None else (job.fetch_globs or [])) if p.strip()]

    # Only as deep as the patterns actually reach: recursion is a more
    # expensive command on the far end, and most fetches want one directory.
    depth = pattern_depth(patterns)
    names = select_files(list_remote_files(transport, job.remote_dir, depth), patterns)
    # The wrapper's own log is not a result: it holds whatever the command
    # wrote to stdout and stderr, while the calculation's real output is the
    # file the command was told to write. It used to be forced into every
    # download, and `*.log` in the default patterns fetched it besides -- so a
    # directory of results carried a job.log next to the .out nobody wanted to
    # tell apart. It stays on the host, where Tail Log reads it live.
    #
    # Never automatically: it is this plugin's file rather than the
    # calculation's output, and a results directory should hold only what the
    # job produced. No wildcard reaches it -- not `*.log`, which is there for
    # Gaussian's output, and not an empty pattern list either.
    #
    # Named exactly it is fetched, because that is somebody asking for it: the
    # download chooser lists it and passes back what was ticked.
    if job.log_file and job.log_file not in patterns:
        names = [name for name in names if name != job.log_file]
    os.makedirs(local_dir, exist_ok=True)
    # Results are downloaded next to the input by default, so the job's own
    # inputs are sitting in the target directory -- and a fetch glob of *.xyz
    # against an input named mol.xyz would otherwise write the remote copy back
    # over the user's file. It is the same bytes today, but a truncated
    # download would destroy the original.
    protected = {os.path.realpath(path) for path in (job.input_files or []) if path}
    downloaded: List[str] = []
    for name in names:
        # Belt and braces: the listing is already filtered, but this is the
        # line that turns a remote string into a local path to write.
        safe = safe_relative_name(name)
        if not safe:
            continue
        target = os.path.join(local_dir, *safe.split("/"))
        # And the check that actually holds: whatever the name looked like, the
        # file has to land inside the directory we were asked to write into.
        root = os.path.realpath(local_dir)
        if not _inside_directory(root, os.path.realpath(os.path.dirname(target))):
            logging.warning("Job Manager: refusing to write outside %s: %r", local_dir, name)
            continue
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.realpath(target) in protected:
            logging.debug("Job Manager: not overwriting the input file %s", name)
            continue
        # Into a part file, then renamed. Results land in the directory the
        # user is working in, so a transfer cut off half way would otherwise
        # leave a truncated .out sitting there under its real name, looking
        # exactly like a complete one -- and over the top of the previous
        # attempt's good copy.
        try:
            _download_atomic(transport, remote_paths.join(job.remote_dir, name), target, local_dir)
        except (TransportError, OSError):
            logging.warning("Job Manager: could not download %s", name)
            continue
        downloaded.append(target)
    return downloaded


def _inside_directory(root: str, path: str) -> bool:
    try:
        return os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:
        # Windows junctions can point to a different drive.
        return False


def _download_atomic(transport: Transport, remote: str, target: str, root: str) -> None:
    """Stage in a private directory; never open a predictable existing link."""
    parent = os.path.realpath(os.path.dirname(target))
    base = os.path.realpath(root)
    if not _inside_directory(base, parent):
        raise TransportError("Download destination escapes the selected directory")
    destination = os.path.join(parent, os.path.basename(target))
    with tempfile.TemporaryDirectory(prefix=".moleditpy-download-", dir=parent) as staging_dir:
        staging = os.path.join(staging_dir, "result")
        transport.download(remote, staging)
        os.replace(staging, destination)


def _discard(path: str) -> None:
    """Remove a part file, if it got as far as existing."""
    try:
        os.unlink(path)
    except OSError:
        logging.debug("Job Manager: part file not removed: %s", path)
