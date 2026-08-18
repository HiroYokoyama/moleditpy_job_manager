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
    queue/      job_0001_<id>.sh, job_0002_<id>.sh, ... run in number order
    running/    the script while its job runs
    pids/       the wrapper pid for each running job
    done/       the script once the job has ended, kept
    status/     the exit code the runner observed, kept
    tmp/        scripts being uploaded, before they are moved into queue/
    sequence    the highest dispatch number ever issued here

**The queue is just numbered shell scripts.** Each one is self-contained -- it
cds into its job directory and runs the wrapper -- so the queue can be read,
reordered or emptied over plain ssh with ``ls`` and ``mv``, and a job that has
run is still exactly the script that ran it. Nothing needs this plugin to make
sense of it.

Five rules make it safe, and each exists because the obvious version is wrong:

**Scripts are uploaded to ``tmp/`` and moved into ``queue/``.** ``mv`` within
one filesystem is atomic, so the runner can never start a half-uploaded script
-- which uploading straight into ``queue/`` would allow.

**The number only ever climbs, and is claimed on the host.** It *is* the
dispatch order. Deriving it from the queue restarted the count as soon as a
user cleared ``done/`` -- which is their disk and their right -- and the next
job then sorted ahead of everything still waiting. The highest ever issued is
kept in ``sequence``, and the new number is one past that or the queue,
whichever is greater. Names are zero padded so a plain ``ls`` reads in order,
and the runner sorts numerically so passing 9999 does not invert it.

**Nothing that is a record is overwritten or deleted.** Entries move
``queue/`` -> ``running/`` -> ``done/`` and stay, and so does ``status/``. What
does go is a finished job's pid file, the lock, and -- in the wrapper, before
its trap is installed -- a stale ``.moleditpy_rc`` from an earlier run in the
same directory, so that a killed run cannot report the previous attempt's exit
code as its own. The runner script is named after a digest of its own contents,
so an upgrade is a new file rather than a rewrite of the one a running runner
is reading by byte offset.

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
#: The unversioned name, kept for the test harnesses and for reading a runner
#: written by a version of this plugin that predates content addressing. What
#: the plugin writes and starts is :func:`runner_script_name`.
RUNNER_SCRIPT_NAME = "moleditpy_runner.sh"
#: Queue entries are bash scripts in this flavour.
ENTRY_SUFFIX = ".sh"
#: The runner's own log, for when a user asks why nothing started.
RUNNER_LOG_NAME = "runner.log"
#: Records which runner script the host already has, so a submission does not
#: upload an identical one every time.
DIGEST_NAME = "runner.sha"

#: Holds the slot count, re-read every pass so the limit can be changed
#: without restarting the runner.
SLOTS_NAME = "slots"
#: What "no limit" means to the helper, which needs a number. High enough never
#: to be the binding constraint, so the core budget is what actually schedules.
UNLIMITED_SLOTS = 9999
#: Holds the number of cores the runner may hand out. Written by the plugin;
#: defaults to the machine's own core count when absent.
CORES_NAME = "cores"
#: Header line by which a job asks for cores.
CORES_TAG = "# moleditpy-cores:"

#: Holds the megabytes of memory the runner may hand out. Absent means the
#: machine's own total; 0 means do not schedule on memory at all.
MEMORY_NAME = "memory"
#: Header line by which a job asks for memory, always in megabytes.
MEMORY_TAG = "# moleditpy-memory:"

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

#: Width of the sequence number. Four digits so a plain ``ls`` reads in order;
#: the runner sorts numerically, so passing 9999 costs nothing but the tidiness
#: of the listing.
SEQUENCE_WIDTH = 4

#: The highest number ever issued on this host. The queue is the other source
#: -- a number in use is obviously taken -- but only this one survives a user
#: clearing ``done/``, and a sequence that restarts puts a new job *ahead* of
#: everything still waiting.
SEQUENCE_NAME = "sequence"

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

    The plugin does not use this -- it claims a number from the host, which is
    the only way to keep counting across a cleared ``done/``. It is kept for
    reading a queue, and for the harnesses that build one.
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
    memory_mb: int = 0,
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
    # Only when there is one: a job with no memory request waits for no memory,
    # which is what a blank field in the wizard has always meant.
    if int(memory_mb or 0) > 0:
        lines.append(f"{MEMORY_TAG} {int(memory_mb)}")
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
    # Physical cores, not hardware threads: `nproc` counts the latter, and a
    # budget of twelve on a six-core machine lets two six-core jobs thrash.
    {CORE_COUNT_SH}
  fi
  positive "$c" 1
}}

total_memory() {{
  m=$(cat {MEMORY_NAME} 2>/dev/null)
  if [ -z "$m" ]; then
    # Nothing found means memory is not scheduled on at all, which is safer
    # than inventing a budget and stalling the queue on it.
    {MEMORY_TOTAL_SH}
  fi
  case "$m" in ''|*[!0-9]*) echo 0 ;; *) echo "$m" ;; esac
}}

header() {{ sed -n "s|^$2 *||p" "$1" 2>/dev/null | head -n 1; }}

job_cores() {{ positive "$(header "$1" '{CORES_TAG}')" 1; }}

# 0 when the job asked for none, which is what a blank Memory field means.
job_memory() {{
  m=$(header "$1" '{MEMORY_TAG}')
  case "$m" in ''|*[!0-9]*) echo 0 ;; *) echo "$m" ;; esac
}}

used_cores() {{
  total=0
  for e in $(ls -1 running 2>/dev/null); do
    total=$((total + $(job_cores "running/$e")))
  done
  echo "$total"
}}

used_memory() {{
  total=0
  for e in $(ls -1 running 2>/dev/null); do
    total=$((total + $(job_memory "running/$e")))
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
  memcap=$(total_memory)
  # Sorted on the number itself, not as text: past 9999 the padding runs out
  # and `sort` puts job_10000 before job_9999, which is the dispatch order
  # inverted at exactly the point a queue has been busy for a long time.
  for entry in $(ls -1 queue 2>/dev/null | sort -t_ -k2,2n); do
    [ "$(count running)" -lt "$(slots)" ] || break
    ready "$entry" || continue
    want=$(job_cores "queue/$entry")
    wantmem=$(job_memory "queue/$entry")
    # A job asking for more than the machine has would otherwise wait for ever;
    # give it everything instead, which means it runs on its own.
    [ "$want" -gt "$cap" ] && want=$cap
    if [ "$memcap" -gt 0 ] && [ "$wantmem" -gt "$memcap" ]; then wantmem=$memcap; fi
    if [ $(($(used_cores) + want)) -gt "$cap" ]; then
      # Strict FIFO: wait for room rather than letting small jobs jump the
      # queue, which would starve anything asking for most of the machine.
      break
    fi
    # Memory is a second budget, checked the same way and for the same reason:
    # two jobs of 90G on a 120G machine must not both start because the cores
    # happened to be free. Overcommitting memory does not merely slow a machine
    # down, it gets a calculation killed hours in.
    if [ "$memcap" -gt 0 ] && [ $(($(used_memory) + wantmem)) -gt "$memcap" ]; then
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


def claim_sequence_command(directory: str) -> str:
    """Take the next dispatch number, and print it. Never goes backwards.

    The number *is* the dispatch order, so it must only ever climb. Deriving it
    from the queue alone did not: clearing ``done/`` -- which a user is entitled
    to do, it is their disk -- restarted the count, and the next job then sorted
    ahead of everything still waiting and jumped the queue.

    So the highest number ever issued is kept on the host as well, and the new
    number is one past whichever is greater. Two clients racing here can come
    away with the same number; that is a tie in the ordering, broken by the job
    id in the name, and not a job running out of turn.
    """
    quoted = quote(directory)
    return (
        "cd " + quoted + " 2>/dev/null || exit 1; "
        "n=$(cat " + SEQUENCE_NAME + " 2>/dev/null); "
        "case \"$n\" in ''|*[!0-9]*) n=0 ;; esac; "
        "for d in queue running done; do "
        'for e in $(ls -1 "$d" 2>/dev/null); do '
        # ${e#job_} then %%_* leaves just the number, with no call to sed.
        "m=${e#job_}; m=${m%%_*}; "
        "case \"$m\" in ''|*[!0-9]*) continue ;; esac; "
        # 10# or a zero-padded number is read as octal, and 0008 is an error.
        'm=$((10#$m)); if [ "$m" -gt "$n" ]; then n=$m; fi; '
        "done; done; "
        "n=$((n+1)); "
        "echo $n > "
        + SEQUENCE_NAME
        + ".tmp && mv -f "
        + SEQUENCE_NAME
        + ".tmp "
        + SEQUENCE_NAME
        + "; echo $n"
    )


def parse_sequence(stdout: str) -> int:
    """The number :func:`claim_sequence_command` printed, or 0 if it said none."""
    for line in reversed((stdout or "").splitlines()):
        token = line.strip()
        if token.isdigit():
            return int(token)
    return 0


def runner_script_name(digest: str) -> str:
    """The runner script's file name for one version of its contents.

    Content-addressed, so a new version is a *new file* and the old one stays
    where it is. Two reasons, and the second is not optional:

    A runner already up is executing that file, and **bash reads a script as it
    goes** -- it seeks by byte offset rather than loading the whole thing. Write
    different contents over a script bash is part way through and it resumes at
    an offset into text that has moved, running whatever fragment now lives
    there. Nothing warns; the queue simply misbehaves.

    And a script that ran a job is worth keeping. The queue is readable over
    plain ssh precisely so that a user can see what ran, and quietly replacing
    the runner underneath a finished batch takes that away.
    """
    return f"moleditpy_runner_{digest}.sh"


def setup_command(directory: str, slots: int, cores: int, memory_mb: int = 0) -> str:
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
    # Printed only when the script that digest names is really still there:
    # reporting a version whose file has been deleted would have the caller
    # skip the upload and then start a runner that does not exist.
    # Absolute, not relying on the cd that prepare_command left behind.
    prefix = quote(f"{directory.rstrip('/')}/moleditpy_runner_")
    report = (
        f"d=$(cat {digest_path} 2>/dev/null); "
        f'if [ -n "$d" ] && [ -f {prefix}"$d".sh ]; then echo "$d"; fi'
    )
    parts = [
        prepare_command(directory),
        set_slots_command(directory, slots),
        set_cores_command(directory, cores),
        set_memory_command(directory, memory_mb),
        report,
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


def ensure_runner_command(directory: str, script_name: str) -> str:
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
        # The script has to be there. nohup reports success for a file that
        # does not exist -- bash fails a moment later, in the background, into
        # the runner log -- so "started" would be a lie and the queue would
        # simply never move.
        f'if [ ! -f "{script_name}" ]; then echo missing; exit 1; fi; '
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


def release_command(directory: str, entry: str) -> str:
    """Let a still-queued job start even though what it waits for did not succeed.

    Cancelling one job in a chain must not throw away the jobs behind it. The
    runner blocks a dependent whose predecessor did not exit 0, and a cancelled
    predecessor leaves no exit code at all -- so without this, cancelling the
    middle job of a chain quietly killed the rest of it.

    The header is rewritten in place, through a temp file: ``sed -i`` needs an
    argument on BSD and macOS and none on GNU, and there is no spelling that
    works on both.
    """
    return (
        f"cd {quote(directory)} 2>/dev/null || exit 0; "
        f'f="queue/{entry}"; [ -f "$f" ] || exit 0; '
        f'sed \'s|^{REQUIRE_SUCCESS_TAG} .*|{REQUIRE_SUCCESS_TAG} 0|\' "$f" > "tmp/{entry}" '
        f'&& mv "tmp/{entry}" "$f" && echo released'
    )


def set_slots_command(directory: str, slots: int) -> str:
    """Change the job limit under a running runner; it re-reads it each pass."""
    return f"cd {quote(directory)} 2>/dev/null && echo {max(1, int(slots))} > {SLOTS_NAME}"


#: Counts *physical* cores, not hardware threads.
#:
#: ``nproc`` reports logical processors, so hyperthreading doubles it: on a
#: six-core machine it says twelve, and a budget of twelve lets two jobs that
#: each asked for six run on six real cores. Quantum chemistry does not gain
#: from that -- it thrashes. lscpu first (Linux), then sysctl (macOS), then
#: /proc/cpuinfo counted as socket+core pairs so a two-socket machine is not
#: counted as one; ``nproc`` only if none of them answered, since a slightly
#: generous budget beats refusing to schedule at all.
CORE_COUNT_SH = (
    "c=$(lscpu -p=core,socket 2>/dev/null | grep -v '^#' | sort -u | wc -l | tr -d ' '); "
    'if [ -z "$c" ] || [ "$c" = "0" ]; then c=$(sysctl -n hw.physicalcpu 2>/dev/null); fi; '
    'if [ -z "$c" ] || [ "$c" = "0" ]; then '
    "c=$(awk -F: '/^physical id/ {p=$2} /^core id/ {print p\"-\"$2}' /proc/cpuinfo 2>/dev/null "
    "| sort -u | wc -l | tr -d ' '); fi; "
    'if [ -z "$c" ] || [ "$c" = "0" ]; then '
    "c=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null); fi"
)

#: Total memory in MB. /proc/meminfo is kB and is on every Linux; sysctl covers
#: macOS. Nothing found leaves it 0, which means "do not schedule on memory".
MEMORY_TOTAL_SH = (
    "m=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null); "
    'if [ -z "$m" ]; then '
    "m=$(sysctl -n hw.memsize 2>/dev/null | awk '{print int($1/1048576)}'); fi"
)

#: Asks the machine what it has, with the runner's own fallbacks, so the number
#: offered in the dialog is the one the queue would have detected for itself.
#: Threads are reported alongside so the dialog can say which is which.
PROBE_RESOURCES = (
    f"{CORE_COUNT_SH}; {MEMORY_TOTAL_SH}; "
    "t=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null); "
    'echo "cores=${c:-0} threads=${t:-0} memory=${m:-0}"'
)


def probe_command() -> str:
    """One command reporting the host's core count and total memory."""
    return PROBE_RESOURCES


def parse_probe(stdout: str) -> tuple:
    """``(cores, memory_mb, threads)`` from :func:`probe_command`; 0 if unread.

    Shared by both flavours, so bash and PowerShell cannot disagree about how
    the answer is spelled.
    """
    found = {"cores": 0, "threads": 0, "memory": 0}
    for line in (stdout or "").splitlines():
        for token in line.split():
            key, _, value = token.partition("=")
            if key in found and value.isdigit():
                found[key] = int(value)
    return found["cores"], found["memory"], found["threads"]


def set_memory_command(directory: str, memory_mb: int) -> str:
    """Change the memory budget, in MB. 0 restores the machine's own total."""
    value = max(0, int(memory_mb or 0))
    path = quote(f"{directory.rstrip('/')}/{MEMORY_NAME}")
    if not value:
        return f"rm -f {path}"
    return f"cd {quote(directory)} 2>/dev/null && echo {value} > {MEMORY_NAME}"


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


__all__: List[str] = [
    "AFTER_TAG",
    "CORES_NAME",
    "CORES_TAG",
    "DIGEST_NAME",
    "MEMORY_NAME",
    "MEMORY_TAG",
    "PAUSED_NAME",
    "UNLIMITED_SLOTS",
    "slots_for",
    "setup_command",
    "store_digest_command",
    "REQUIRE_SUCCESS_TAG",
    "STATUS_BLOCKED",
    "release_command",
    "is_paused_command",
    "pause_command",
    "prepare_command",
    "set_cores_command",
    "set_memory_command",
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
