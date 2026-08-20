"""Point a new job's input at a file a previous job already produced.

Purely generic: this inserts a *filename*, nothing else. What it means to the
program that reads it (an ORCA ``* xyzfile`` block, a Gaussian ``%oldchk``,
...) is between the input and that program; this parses no chemistry.

Only relays between jobs on the *same host*, via a single remote ``cp`` /
``Copy-Item`` -- nothing is downloaded and re-uploaded, and cross-host is not
supported since there's no way to name one host's path on another.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from typing import List

from .models import STATE_DONE, Job

#: Left on disk (not cleaned up): the upload runs on a worker thread with no
#: cheap hook back to "the transfer is done".
RELAY_DIRNAME = "moleditpy_job_manager_relay"

#: ``[prevfile]`` or ``[prevfile:.ext]``, resolved from another job's result at
#: submit time. Square brackets, not the {input}/{stem} braces: those are
#: substituted into the command line, this into file *content*.
#:
#: The nested form ``[prevfile:.res/.xyz]`` is for a file inside its own
#: subdirectory: extension before the slash names the folder, after names the
#: file, both appended to the same stem.
_EXT = r"\.[A-Za-z0-9]+"
TAG_RE = re.compile(rf"\[prevfile(?::(?P<ext>{_EXT}(?:/{_EXT})?))?\]")


class StructureRelayError(ValueError):
    """The tag, the source job or the file it names cannot be resolved."""


def find_tags(text: str) -> List[re.Match]:
    return list(TAG_RE.finditer(text or ""))


def candidate_jobs(jobs, host_id: str = "") -> List[Job]:
    """Jobs a relay could plausibly come from: finished successfully, or
    still going.

    A still-active job is offered too: relaying from it chains the new
    submission behind it (see :mod:`runner`) instead of making the user come
    back later. Jobs with no remote directory yet, or on a different host when
    ``host_id`` is given, are excluded.
    """
    found = [
        job
        for job in jobs
        if job.remote_dir
        and (job.state == STATE_DONE or job.is_active)
        and (not host_id or job.host_id == host_id)
    ]
    return sorted(found, key=lambda job: job.updated_at, reverse=True)


def _stem_of(job: Job) -> str:
    """What a relayed file is most likely named after.

    ORCA (etc) writes outputs from the input file's own base name, so the
    uploaded input's stem is tried first, falling back to the job's own name.
    """
    if job.input_files:
        return os.path.splitext(os.path.basename(job.input_files[0]))[0]
    return job.name


def resolve_filename(job: Job, ext: str) -> str:
    """The filename (or nested path) a relay for ``ext`` most likely
    resolves to.

    A guess by convention, not inspection; checked for real at submit time, so
    a missing file fails with a clear message rather than silently.
    ``ext`` containing ``/`` (``.res/.xyz``) names a file one directory down.
    """
    stem = _stem_of(job)
    if "/" in ext:
        folder_ext, file_ext = ext.split("/", 1)
        return f"{stem}{folder_ext}/{stem}{file_ext}"
    return f"{stem}{ext}"


def substitute_paths(text: str, job: Job) -> str:
    """Replace every ``[prevfile]`` / ``[prevfile:.ext]`` in ``text``.

    Raises :class:`StructureRelayError` if there is no tag, or a tag with no
    extension (that form is for manual editing; nothing here can guess).
    """
    matches = find_tags(text)
    if not matches:
        raise StructureRelayError(
            f"No {TAG_RE.pattern} tag was found, so there is nothing to fill in."
        )
    result = text
    for match in reversed(matches):
        ext = match.group("ext")
        if not ext:
            raise StructureRelayError(
                "[prevfile] needs an extension to know which file is meant, "
                "e.g. [prevfile:.chk] or [prevfile:.xyz]."
            )
        filename = resolve_filename(job, ext)
        result = result[: match.start()] + filename + result[match.end() :]
    return result


def relay_plan(text: str, job: Job) -> List[str]:
    """The filenames that have to be copied out of the source job's directory.

    One per distinct extension the input asks for. Relayed under the same
    name in both directories, matching what the text substitution wrote.
    """
    seen: List[str] = []
    for match in find_tags(text):
        ext = match.group("ext")
        if not ext:
            continue
        filename = resolve_filename(job, ext)
        if filename not in seen:
            seen.append(filename)
    return seen


def materialize(local_path: str, job: Job) -> str:
    """A temporary copy of ``local_path`` with every tag replaced by a
    filename.

    Same basename, in a directory of its own, so nothing else needs to know a
    substitution happened. The original is never written to.
    """
    try:
        with open(local_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise StructureRelayError(f"Could not read {local_path}: {exc}") from exc
    filled = substitute_paths(text, job)

    # One directory per call: keying on clock+pid alone let two same-second,
    # same-basename inputs collide and overwrite each other. Timestamp stays
    # in the prefix since the directory is never cleaned up and should be
    # readable by hand.
    root = os.path.join(tempfile.gettempdir(), RELAY_DIRNAME)
    os.makedirs(root, exist_ok=True)
    directory = tempfile.mkdtemp(prefix=f"{int(time.time())}_{os.getpid()}_", dir=root)
    target = os.path.join(directory, os.path.basename(local_path))
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(filled)
    except OSError as exc:
        raise StructureRelayError(f"Could not write {target}: {exc}") from exc
    return target


__all__ = [
    "RELAY_DIRNAME",
    "TAG_RE",
    "StructureRelayError",
    "candidate_jobs",
    "find_tags",
    "materialize",
    "relay_plan",
    "resolve_filename",
    "substitute_paths",
]
