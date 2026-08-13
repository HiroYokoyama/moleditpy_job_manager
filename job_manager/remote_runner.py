"""A tiny job queue that lives on the remote machine.

``nohup`` is not a scheduler: on a host with no queue nothing decides what runs
when. Chained lanes (see ``store.chain_lane_tail``) fix that without leaving
anything on the host, but the order is fixed at submit time -- a job that
finishes early cannot let the next one start, and a cancelled job leaves a gap
in its chain.

This module is the other option: a small bash runner holding a real FIFO queue.
It exits the moment the queue empties, so nothing is left running on a shared
login node between batches.

Layout under ``<remote_root>/.moleditpy_runner/``::

    lock/       the single-instance lock (an atomic mkdir), holding pid
    queue/      job_0001_<id>.sh, job_0002_<id>.sh, ... run in name order
    running/    the script while its job runs
    pids/       the wrapper pid for each running job
    done/       the script once the job has ended
    tmp/        scripts being uploaded, before they are moved into queue/

**The queue is just numbered shell scripts.** Each one is self-contained -- it
cds into its job directory and runs the wrapper -- so the queue can be read,
reordered or emptied over plain ssh with ``ls`` and ``mv``, and a job that has
run is still exactly the script that ran it. Nothing needs this plugin to make
sense of it.

Four rules make it safe, and each exists because the obvious version is wrong:

**Scripts are uploaded to ``tmp/`` and moved into ``queue/``.** ``mv`` within
one filesystem is atomic, so the runner can never start a half-uploaded script
-- which uploading straight into ``queue/`` would allow.

**Numbers are zero padded.** ``ls | sort`` puts ``job_10`` before ``job_2``, so
unpadded names would dispatch in the wrong order as soon as there were ten
jobs. The job id in the name keeps two clients from colliding on one number.

**A job is claimed by moving it out of ``queue/``.** Two runners racing for the
same entry cannot both win a ``mv``, so nothing is ever dispatched twice.

**The runner re-checks the queue after releasing its lock.** Otherwise a job
enqueued between "the queue is empty" and "the lock is gone" would sit there
with nobody left to run it: whoever enqueued it saw a live runner and so did
not start one. Having released the lock, the runner looks again and takes it
back if something arrived. This is the whole reason a runner that exits
immediately is safe -- without it, exiting on an empty queue silently drops
work.
"""

from __future__ import annotations

import re
import sys
from typing import List, Sequence

from .remote_paths import quote

#: Directory the runner keeps its state in, under the host's remote root.
RUNNER_DIRNAME = ".moleditpy_runner"
#: Name of the generated runner script.
RUNNER_SCRIPT_NAME = "moleditpy_runner.sh"
#: Queue entries are bash scripts in this flavour.
ENTRY_SUFFIX = ".sh"
#: The runner's own log, for when a user asks why nothing started.
RUNNER_LOG_NAME = "runner.log"
#: Holds the slot count, re-read every pass so the limit can be changed
#: without restarting the runner.
#: Records which runner script the host already has, so a submission does not
#: upload an identical one every time.
DIGEST_NAME = "runner.sha"

SLOTS_NAME = "slots"
#: What "no limit" means to the helper, which needs a number. High enough never
#: to be the binding constraint, so the core budget is what actually schedules.
UNLIMITED_SLOTS = 9999
#: Holds the number of cores the runner may hand out. Written by the plugin;
#: defaults to the machine's own core count when absent.
CORES_NAME = "cores"
#: Header line by which a job asks for cores.
CORES_TAG = "# moleditpy-cores:"

#: How often the runner reaps finished jobs and dispatches waiting ones. Every
#: check is a shell builtin or one ``ls``, so this costs nothing measurable.
RUNNER_POLL_SECONDS = 5

#: Sub-directories the runner needs.
SUBDIRS = ("queue", "running", "done", "pids", "tmp", "status")

#: Presence of this file stops the runner dispatching anything new. Running
#: jobs are left alone -- pausing a queue must never kill work in progress.
PAUSED_NAME = "paused"

#: Header lines a queued script may carry. They are comments, so the script
#: still runs by hand, and the runner reads them with one `sed`.
AFTER_TAG = "# moleditpy-after:"
REQUIRE_SUCCESS_TAG = "# moleditpy-require-success:"

#: Written into ``status/<entry>`` when a job can never run: its dependency
#: failed, or was never queued at all.
STATUS_BLOCKED = "blocked"

#: Width of the sequence number. Four digits keeps ``sort`` honest to 9999
#: jobs, which is well past the point where a login node is the wrong tool.
SEQUENCE_WIDTH = 4

#: Both flavours' queue entries. The suffix says which shell runs it; the
#: number and the job id mean the same thing in each.
_ENTRY_RE = re.compile(r"^job_(\d+)_([A-Za-z0-9]+)\.(?:sh|ps1)$")


def flavour_for(host):
    """The runner implementation this host's shell can actually execute.

    Two modules, one set of names: the constants, the entry format and the
    listing parser are shared, so bash and PowerShell cannot drift apart on
    what a queue entry is called or what a header means.
    """
    from .models import SCHEDULER_WINDOWS

    if getattr(host, "scheduler", "") == SCHEDULER_WINDOWS:
        from . import remote_runner_ps

        return remote_runner_ps
    return sys.modules[__name__]


def slots_for(host) -> int:
    """How many jobs the helper on ``host`` may run at once.

    ``max_concurrent`` of 0 means "no limit" everywhere else in the plugin, and
    the helper needs a number. Passing 1 -- which is what ``or 1`` did -- turned
    the default host profile into a strictly serial queue: a user who had never
    set a limit got one job at a time, from a control that said *no limit*, with
    nothing on screen to explain it.

    With no job limit the core budget is the constraint instead, which is the
    whole point of runner mode: two jobs asking for four cores each run together
    on an eight-core machine, and a third waits for cores rather than for a
    slot it was never told about.
    """
    limit = max(0, int(getattr(host, "max_concurrent", 0) or 0))
    return limit or UNLIMITED_SLOTS


def runner_dir(remote_root: str) -> str:
    """The runner's directory under a host's remote root."""
    root = (remote_root or "~/moleditpy_jobs").rstrip("/")
    return f"{root}/{RUNNER_DIRNAME}"


def entry_name(sequence: int, job_id: str, suffix: str = ".sh") -> str:
    """``job_0007_a1b2c3d4.sh`` -- sortable, and unique per job."""
    return f"job_{int(sequence):0{SEQUENCE_WIDTH}d}_{job_id}{suffix}"


def parse_entry(name: str) -> tuple:
    """``(sequence, job_id)`` for a queue entry, or ``(0, "")`` if unreadable."""
    match = _ENTRY_RE.match((name or "").strip())
    if not match:
        return (0, "")
    return (int(match.group(1)), match.group(2))


def next_sequence(existing: Sequence[str]) -> int:
    """One past the highest number already used, across every directory.

    Counted over queue, running *and* done: reusing the number of a finished
    job would put a new job ahead of everything waiting, since the number is
    the dispatch order.
    """
    highest = 0
    for name in existing or ():
        highest = max(highest, parse_entry(name)[0])
    return highest + 1


def build_job_script(
    job_dir: str,
    script_name: str,
    log_name: str,
    entry: str,
    directory: str,
    job_name: str = "",
    after_job_id: str = "",
    require_success: bool = True,
    cores: int = 1,
) -> str:
    """The small script the queue holds for one job.

    Self-contained on purpose: the runner does not interpret the body, it just
    runs it, so there is no format to get wrong and nothing for a job name to
    escape from. What the runner *does* read is the comment header -- how many
    cores the job wants, and what it has to wait for -- which keeps the script
    runnable by hand.

    The exit code is written to ``status/<entry>`` because the runner has no
    other way to know it: the wrapper's own sentinel lives in the job
    directory, which the runner does not read.
    """
    lines = ["#!/bin/bash"]
    if job_name:
        lines.append(f"# MoleditPy job: {job_name}")
    lines.append(f"{CORES_TAG} {max(1, int(cores or 1))}")
    if after_job_id:
        lines.append(f"{AFTER_TAG} {after_job_id}")
        lines.append(f"{REQUIRE_SUCCESS_TAG} {1 if require_success else 0}")
    status_path = f"{directory.rstrip('/')}/status/{entry}"
    lines += [
        f"cd {quote(job_dir)} || exit 1",
        f"bash {quote(script_name)} > {quote(log_name)} 2>&1",
        "__moleditpy_rc=$?",
        f'echo "$__moleditpy_rc" > {quote(status_path)}',
        "exit $__moleditpy_rc",
        "",
    ]
    return "\n".join(lines)


def build_runner_script(directory: str, poll_seconds: int = RUNNER_POLL_SECONDS) -> str:
    """The runner itself, with its own directory baked in.

    ``$0`` is not used to find that directory: the script is started under
    nohup from a shell whose working directory is not guaranteed, and the same
    assumption in the job wrappers made a real queue write its sentinel into a
    spool directory that had already been deleted.
    """
    quoted = quote(directory)
    # Fractional allowed: production passes 5, and the tests that drive a real
    # runner would otherwise pay a whole second per dispatch.
    poll = max(0.1, float(poll_seconds))
    return f"""#!/bin/bash
# MoleditPy remote job runner. Runs the scripts in queue/ in name order, at
# most `slots` at a time, and exits as soon as there is nothing left to run.
cd {quoted} || exit 1

count() {{ ls -1 "$1" 2>/dev/null | wc -l | tr -d ' '; }}

positive() {{ case "$1" in ''|*[!0-9]*|0) echo "$2" ;; *) echo "$1" ;; esac; }}

slots() {{
  # Re-read every pass, so the limits can be changed without a restart.
  positive "$(cat {SLOTS_NAME} 2>/dev/null)" 1
}}

total_cores() {{
  c=$(cat {CORES_NAME} 2>/dev/null)
  if [ -z "$c" ]; then
    c=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)
  fi
  positive "$c" 1
}}

header() {{ sed -n "s|^$2 *||p" "$1" 2>/dev/null | head -n 1; }}

job_cores() {{ positive "$(header "$1" '{CORES_TAG}')" 1; }}

used_cores() {{
  total=0
  for e in $(ls -1 running 2>/dev/null); do
    total=$((total + $(job_cores "running/$e")))
  done
  echo "$total"
}}

# Where a job id currently is, as "<dir> <entry>". Nothing printed if the job
# was never queued here at all.
find_entry() {{
  for d in queue running done; do
    e=$(ls -1 "$d" 2>/dev/null | grep -- "_$1\\.sh$" | head -n 1)
    if [ -n "$e" ]; then echo "$d $e"; return 0; fi
  done
  return 1
}}

block() {{ mv "queue/$1" "done/$1" 2>/dev/null && echo {STATUS_BLOCKED} > "status/$1"; }}

reap() {{
  for entry in $(ls -1 running 2>/dev/null); do
    pid=$(cat "pids/$entry" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      continue
    fi
    # An empty pid file means the job could not be started at all; either way
    # it is over, and leaving it in running/ would hold its cores for ever.
    mv "running/$entry" "done/$entry" 2>/dev/null
    rm -f "pids/$entry"
  done
}}

# True when everything $1 waits for has happened. A job whose dependency can
# never be satisfied is moved aside rather than left in the queue: it would
# otherwise keep this runner alive for ever, and a runner that exits when the
# queue empties must have no way to be stuck with an immortal queue.
ready() {{
  after=$(header "queue/$1" '{AFTER_TAG}')
  [ -z "$after" ] && return 0
  loc=$(find_entry "$after") || {{ block "$1"; return 1; }}
  set -- "$1" $loc
  [ "$2" = "done" ] || return 1
  need=$(header "queue/$1" '{REQUIRE_SUCCESS_TAG}')
  [ "$need" = "1" ] || return 0
  rc=$(cat "status/$3" 2>/dev/null)
  if [ "$rc" = "0" ]; then return 0; fi
  block "$1"
  return 1
}}

dispatch() {{
  # Pausing stops new work only. Killing what is already running would make
  # "pause" mean "throw away the last six hours".
  [ -f {PAUSED_NAME} ] && return 0
  cap=$(total_cores)
  for entry in $(ls -1 queue 2>/dev/null | sort); do
    [ "$(count running)" -lt "$(slots)" ] || break
    ready "$entry" || continue
    want=$(job_cores "queue/$entry")
    # A job asking for more than the machine has would otherwise wait for ever;
    # give it everything instead, which means it runs on its own.
    [ "$want" -gt "$cap" ] && want=$cap
    if [ $(($(used_cores) + want)) -gt "$cap" ]; then
      # Strict FIFO: wait for room rather than letting small jobs jump the
      # queue, which would starve anything asking for most of the machine.
      break
    fi
    # Claiming a job *is* moving it: two runners cannot both win this mv, so
    # no job is ever dispatched twice.
    mv "queue/$entry" "running/$entry" 2>/dev/null || continue
    # The job script redirects its own output, so the wrapper's streams go
    # nowhere. The braces matter: `A && nohup B & echo $!` backgrounds the
    # whole list, and the subshell then holds this runner's stdout open until
    # the job ends -- which would stall the queue behind it.
    ( {{ nohup bash "running/$entry" > /dev/null 2>&1 < /dev/null & }} && echo $! ) \\
      > "pids/$entry" 2>/dev/null
  done
}}

while :; do
  reap
  dispatch
  if [ "$(count running)" -eq 0 ] && [ "$(count queue)" -eq 0 ]; then
    rm -rf lock
    # Look again now the lock is gone. A job enqueued between the test above
    # and the release would otherwise sit in the queue for ever: whoever put
    # it there saw a live runner, and so did not start one.
    if [ "$(count queue)" -eq 0 ]; then
      exit 0
    fi
    # Something arrived. Take the lock back -- unless a new runner beat us to
    # it, in which case that one will dispatch the job and this one is done.
    mkdir lock 2>/dev/null || exit 0
    echo $$ > lock/pid
    continue
  fi
  sleep {poll:g}
done
"""


def prepare_command(directory: str) -> str:
    """Create the runner's directories. Safe to repeat."""
    subdirs = " ".join(f'"{name}"' for name in SUBDIRS)
    return f"mkdir -p {quote(directory)} && cd {quote(directory)} && mkdir -p {subdirs}"


def setup_command(directory: str, slots: int, cores: int) -> str:
    """Everything a submission has to settle before queueing, in one call.

    Four round trips became one. Each of them was a separate ``ssh`` process,
    and the OpenSSH backend cannot multiplex on Windows -- so on that platform
    they were four full handshakes on the way to every single submission.

    Prints the digest of the runner script already on the host, or nothing:
    that is what lets the caller skip re-uploading a script the host already
    has, which is the last of the fixed costs and the only one that is an scp.
    """
    # Built before the f-string, not inside it: nesting the same quote inside
    # an f-string expression is Python 3.12 syntax, and this package supports
    # 3.9. The same shape has already shipped a module that would not import.
    digest_path = quote(f"{directory.rstrip('/')}/{DIGEST_NAME}")
    parts = [
        prepare_command(directory),
        set_slots_command(directory, slots),
        set_cores_command(directory, cores),
        f"cat {digest_path} 2>/dev/null || true",
    ]
    return "; ".join(parts)


def store_digest_command(directory: str, digest: str) -> str:
    """Record which runner script is on the host, after uploading it."""
    return f"cd {quote(directory)} && echo {quote(digest)} > {DIGEST_NAME}"


def list_command(directory: str) -> str:
    """Every entry the runner knows about, as ``<state> <entry>`` lines.

    One command covers every job on the host -- the same contract the
    queue-based schedulers meet with a single ``squeue``.
    """
    return (
        f"cd {quote(directory)} 2>/dev/null || exit 0; "
        "for d in queue running done; do "
        'for e in $(ls -1 "$d" 2>/dev/null); do echo "$d $e"; done; '
        "done"
    )


def parse_listing(stdout: str) -> dict:
    """``{job_id: "queue"|"running"|"done"}`` from :func:`list_command`."""
    states = {}
    for line in (stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        where, entry = parts
        job_id = parse_entry(entry)[1]
        if job_id and where in ("queue", "running", "done"):
            states[job_id] = where
    return states


def enqueue_command(directory: str, entry: str) -> str:
    """Move an uploaded script from ``tmp/`` into ``queue/``."""
    return f'cd {quote(directory)} && mv "tmp/{entry}" "queue/{entry}"'


def ensure_runner_command(directory: str, script_name: str = RUNNER_SCRIPT_NAME) -> str:
    """One command that guarantees a runner is up, and is safe to repeat.

    Run *after* the job is in the queue, never before: a runner started first
    can empty the queue and exit before the job arrives, whereas one started
    afterwards always sees it.

    A lock whose pid is dead is reclaimed -- a runner killed with SIGKILL, or a
    rebooted machine, would otherwise lock the host out of its own queue for
    ever. The pid is written by the shell that started the runner, so the lock
    is never briefly present without one.
    """
    return (
        f"cd {quote(directory)} 2>/dev/null || exit 1; "
        # Reclaim a lock left behind by a runner that is no longer alive.
        "if [ -d lock ]; then p=$(cat lock/pid 2>/dev/null); "
        'if [ -z "$p" ] || ! kill -0 "$p" 2>/dev/null; then rm -rf lock; fi; fi; '
        # mkdir is the lock: atomic, and unlike flock it behaves on NFS, which
        # is where a cluster's home directory usually lives.
        "if mkdir lock 2>/dev/null; then "
        f'{{ nohup bash "{script_name}" > "{RUNNER_LOG_NAME}" 2>&1 < /dev/null & }} '
        "&& echo $! > lock/pid && echo started; "
        "else echo running; fi"
    )


def cancel_command(directory: str, entry: str) -> str:
    """Cancel whether the job is waiting or already running.

    A waiting job is cancelled by taking it out of the queue, which frees its
    slot at once -- the thing chained lanes cannot do.
    """
    return (
        f"cd {quote(directory)} 2>/dev/null || exit 0; "
        f'if mv "queue/{entry}" "done/{entry}" 2>/dev/null; then echo dequeued; exit 0; fi; '
        f'p=$(cat "pids/{entry}" 2>/dev/null); '
        '[ -n "$p" ] || exit 0; '
        # Kill the process group, so the payload dies with its wrapper.
        'kill -- -$(ps -o pgid= -p "$p" 2>/dev/null | tr -d " ") 2>/dev/null || kill "$p"'
    )


def set_slots_command(directory: str, slots: int) -> str:
    """Change the job limit under a running runner; it re-reads it each pass."""
    return f"cd {quote(directory)} 2>/dev/null && echo {max(1, int(slots))} > {SLOTS_NAME}"


def set_cores_command(directory: str, cores: int) -> str:
    """Change how many cores the runner may hand out. 0 restores ``nproc``."""
    value = max(0, int(cores))
    path = quote(f"{directory.rstrip('/')}/{CORES_NAME}")
    if not value:
        return f"rm -f {path}"
    return f"cd {quote(directory)} 2>/dev/null && echo {value} > {CORES_NAME}"


def pause_command(directory: str, paused: bool) -> str:
    """Hold the queue, or let it move again.

    Only new dispatches are held: a pause that killed running jobs would mean
    "throw away the last six hours", which is not what anyone means by it.
    """
    path = quote(f"{directory.rstrip('/')}/{PAUSED_NAME}")
    return f"touch {path}" if paused else f"rm -f {path}"


def is_paused_command(directory: str) -> str:
    """Prints ``paused`` or ``running``."""
    path = quote(f"{directory.rstrip('/')}/{PAUSED_NAME}")
    return f"if [ -f {path} ]; then echo paused; else echo running; fi"


def status_of(stdout: str) -> str:
    """The exit code a finished job recorded, or ``blocked``/``""``."""
    return (stdout or "").strip()


__all__: List[str] = [
    "AFTER_TAG",
    "CORES_NAME",
    "CORES_TAG",
    "DIGEST_NAME",
    "PAUSED_NAME",
    "UNLIMITED_SLOTS",
    "slots_for",
    "setup_command",
    "store_digest_command",
    "REQUIRE_SUCCESS_TAG",
    "STATUS_BLOCKED",
    "is_paused_command",
    "pause_command",
    "prepare_command",
    "set_cores_command",
    "status_of",
    "RUNNER_DIRNAME",
    "RUNNER_LOG_NAME",
    "RUNNER_POLL_SECONDS",
    "RUNNER_SCRIPT_NAME",
    "SEQUENCE_WIDTH",
    "SLOTS_NAME",
    "SUBDIRS",
    "build_job_script",
    "build_runner_script",
    "cancel_command",
    "enqueue_command",
    "ensure_runner_command",
    "entry_name",
    "list_command",
    "next_sequence",
    "parse_entry",
    "parse_listing",
    "prepare_command",
    "runner_dir",
    "set_slots_command",
]
