"""Point a new job's input at a file a previous job already produced.

The case: an optimisation finishes, and the next job -- a frequency
calculation, a single point at a better level of theory, a Gaussian job
picking up where an old checkpoint left off -- should read something the
first job wrote, without the user copying a path around by hand.

What this module does is entirely generic, on purpose: it inserts a
*filename*, nothing else. What that filename means to the program that reads
it -- an ORCA ``* xyzfile`` block, a Gaussian ``%oldchk``, anything else -- is
between the input file and the program; this has no opinion on it and parses
no chemistry. That is left to whichever plugin wrote the tag in the first
place, which is also the one place that knows what its own program expects.

The relay only ever happens between jobs on the *same host*. The file is
moved there with a single remote command -- ``cp`` or ``Copy-Item`` -- so
nothing is downloaded to this machine and re-uploaded; a relay across two
different hosts is not offered, because there is no way to name a path on one
host that means anything on the other without moving the bytes through here,
which is a very different (and slower, and more failure-prone) operation this
does not attempt.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from typing import List

from .models import STATE_DONE, Job

#: Where a substituted copy of an input file is written before upload. Left
#: on disk rather than cleaned up after: the upload happens on a worker
#: thread dispatched well after this file is written, and there is no hook
#: back to "the transfer is done" cheap enough to justify chasing it for a
#: few kilobytes of text.
RELAY_DIRNAME = "moleditpy_job_manager_relay"

#: What an input file writes in place of a filename, to be resolved from
#: another job's result at submit time: ``[prevfile]`` alone, or
#: ``[prevfile:.ext]`` naming which of that job's files is wanted. Square
#: brackets rather than the curly braces {input}/{stem} already use: those
#: are substituted into the *command line*, this into the *file content* --
#: two different passes, and a shape that cannot be confused with either.
#:
#: A second, nested form -- ``[prevfile:.res/.xyz]`` -- is for the file that
#: is not a sibling of the input at all but sits inside a directory of its
#: own, wherever a particular workflow puts one: the extension before the
#: slash names the folder, the one after names the file inside it, both still
#: just an extension appended to the same stem, which is all this ever
#: computes. Not a claim about what any one program does by default -- the
#: plugin that writes the tag is the one that knows its own layout.
_EXT = r"\.[A-Za-z0-9]+"
TAG_RE = re.compile(rf"\[prevfile(?::(?P<ext>{_EXT}(?:/{_EXT})?))?\]")


class StructureRelayError(ValueError):
    """The tag, the source job or the file it names cannot be resolved."""


def find_tags(text: str) -> List[re.Match]:
    return list(TAG_RE.finditer(text or ""))


def candidate_jobs(jobs, host_id: str = "") -> List[Job]:
    """Jobs a relay could plausibly come from: finished successfully, or
    still going.

    A job that has not finished yet is offered too, not only a DONE one --
    relaying from it chains the new submission behind it and copies the file
    once it is actually there (see :mod:`runner`), rather than making the
    user come back to the wizard a second time once it has. A job that ended
    badly (FAILED, CANCELLED, LOST) is not offered: there is nothing there
    worth reading.

    Excluded regardless of state if there is no remote directory yet -- a job
    still UPLOADING has nowhere for a relay to point at.

    Restricted to ``host_id`` when it is given -- a relay only ever moves a
    file within one host, so a job on a different one is not a candidate at
    all, not merely a worse one.
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

    ORCA writes every output from the *input file's* own base name, whatever
    the job is called in this plugin's table, so the uploaded input's stem is
    tried first; a job resubmitted with no input file at all (work already on
    the host) falls back to the job's own name, which is the best guess left.
    """
    if job.input_files:
        return os.path.splitext(os.path.basename(job.input_files[0]))[0]
    return job.name


def resolve_filename(job: Job, ext: str) -> str:
    """The filename (or nested path) a relay for ``ext`` most likely
    resolves to.

    Convention, not inspection: nothing here reads the old job's input to see
    what it actually named its checkpoint, because a program-specific
    directive is exactly the "software specific type" this module does not
    parse. The guess is checked for real at submit time -- a remote copy of a
    path that is not there fails with a clear message rather than silently
    producing a job with nothing to read.

    ``ext`` containing a ``/`` (``.res/.xyz``) names a file one directory
    down, itself named after the same stem -- see :data:`TAG_RE`. The result
    always uses ``/``, which every dialect here already accepts as a path
    separator for the same reason ``remote_paths.join`` does.
    """
    stem = _stem_of(job)
    if "/" in ext:
        folder_ext, file_ext = ext.split("/", 1)
        return f"{stem}{folder_ext}/{stem}{file_ext}"
    return f"{stem}{ext}"


def substitute_paths(text: str, job: Job) -> str:
    """Replace every ``[prevfile]`` / ``[prevfile:.ext]`` in ``text``.

    Raises :class:`StructureRelayError` if the text names no tag at all, or if
    a tag with no extension is used -- that form exists for a human editing
    the input by hand and choosing the file themselves elsewhere; nothing here
    can guess which file was meant without one.
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

    One per distinct extension the input asks for -- a Gaussian input naming
    both ``.chk`` and something else in two tags copies two files, not one.
    Relayed under the same name in both directories, so this is the file's
    name relative to *either* job's own directory: where it comes from is
    resolved once the new job's directory exists (see :mod:`runner`), and the
    text substitution above already wrote this exact name into the input.
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

    Same basename as the original, in a directory of its own, so nothing
    else -- ``{input}``/``{stem}``, the fetch patterns, the job's default
    name -- has to know a substitution happened; only its content differs.
    The original is never written to.
    """
    try:
        with open(local_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise StructureRelayError(f"Could not read {local_path}: {exc}") from exc
    filled = substitute_paths(text, job)

    # One directory per call, not one per second per process. Keyed on the
    # clock and the pid, two files materialised in the same second shared a
    # directory -- and two inputs with the same basename, which is the ordinary
    # case for a job assembled from several folders, resolved to one path: the
    # second overwrote the first, and the job uploaded that one twice under
    # both names. The timestamp stays in the prefix, because the directory is
    # deliberately never cleaned up and wants to be readable by hand.
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
