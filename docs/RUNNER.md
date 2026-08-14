# The helper queue on the host

`nohup` is not a scheduler. On a machine with no queue — your workstation, a
group server, a login node you are allowed to compute on — nothing stops five
submissions starting at once and fighting over the same cores and the same
memory.

The helper is a small script *on the host* holding a real FIFO queue. It is the
default for such a host, and is never offered where a scheduler already exists:
a second queue on a SLURM login node is both pointless and the thing sysadmins
object to.

This document is the whole design. For using it, see
[WORKFLOW.md](WORKFLOW.md); for where it sits in the plugin, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Layout

Everything lives under `<remote_root>/.moleditpy_runner/`:

```
lock/       single-instance lock (an atomic mkdir), holding the runner's pid
queue/      job_0001_<id>.sh, job_0002_<id>.sh …   waiting
running/    the same script, while its job runs
done/       the same script, once it has ended        (kept)
status/     the exit code the runner observed         (kept)
pids/       the wrapper pid of each running job
tmp/        scripts mid-upload, before they are moved into queue/
slots  cores  memory  paused  sequence  runner.sha
moleditpy_runner_<digest>.sh
```

**A job's directory is its state.** Nothing tracks it separately — where the
file sits *is* the answer, which is why the whole queue can be read, reordered
or emptied over plain `ssh` with `ls` and `mv`. Nothing needs this plugin to
make sense of it.

## The entry name: `job_0007_a1b2c3d4e5f6.sh`

Two halves, deliberately not the same thing.

| Half | Is | Comes from | Answers |
|---|---|---|---|
| `0007` | the dispatch number | claimed from the host's `sequence` | *when* — position in the queue |
| `a1b2c3d4e5f6` | the job id | the job record, a `uuid4` prefix | *who* — which job in your list |

Neither can do the other's work:

- **A uuid has no order.** Sorting on it would dispatch at random, so the queue
  needs a number that counts.
- **A number is not an identity.** It is claimed on the host, at submit time,
  *after* the job record already exists — and two clients racing can be handed
  the same one. The id is what makes the filename unique regardless, which is
  why that race is a tie in the ordering rather than a collision.
- **The plugin maps entries back to jobs by id.** One listing per poll returns
  `<dir> <entry>` lines, and the id in the name is what turns those into job
  states without opening a single file.
- **Dependencies are by id too.** `# moleditpy-after:` names a job, and the
  runner finds it with `ls | grep -- "_<id>.sh$"` across `queue/`, `running/`
  and `done/`. A number would be no use here: a job does not know its
  predecessor's queue position, and that position is meaningless once the
  predecessor has run.

So the number orders the queue and the id identifies the job, and the name
carries both because the runner needs to do both things with an `ls`.

## What a queue entry is

One self-contained script per job:

```bash
#!/bin/bash
# MoleditPy job: opt
# moleditpy-cores: 8
# moleditpy-memory: 24000
# moleditpy-after: a1b2c3d4e5f6
# moleditpy-require-success: 1
cd /home/you/moleditpy_jobs/20260814_101500_opt_a1b2c3d4e5f6 || exit 1
bash moleditpy_run.sh > job.log 2>&1
__moleditpy_rc=$?
echo "$__moleditpy_rc" > /home/you/moleditpy_jobs/.moleditpy_runner/status/job_0007_a1b2c3d4e5f6.sh
exit $__moleditpy_rc
```

The header is **comments**, so the script still runs by hand — and it is how the
runner learns what the job wants, read with one `sed`. The exit code goes to
`status/` because the runner has no other way to learn it: the wrapper's own
sentinel lives in the job directory, which the runner never reads.

`moleditpy_run.sh` is exactly the wrapper the no-queue scheduler builds — same
`.moleditpy_rc` sentinel, same signal traps. Completion is therefore detected
the same way for every backend; see
[ARCHITECTURE.md](ARCHITECTURE.md#how-a-jobs-completion-is-detected).

## Submitting

Five commands and three transfers, for a host that already has the runner:

1. `mkdir` the job's own directory — `<date>_<name>_<job id>` — and upload the
   inputs.
2. Upload the wrapper.
3. One **setup** call: create the sub-directories, write `slots`, `cores` and
   `memory`, and report which runner script the host already has.
4. **Claim a number** from the host's `sequence` counter.
5. Upload the queue entry to `tmp/`, then `mv` it into `queue/`.
6. **Ensure a runner is up** — always last; see below.

The runner script is a fourth transfer only when its digest differs.
`build_runner_script` is deterministic, so that is one extra file per plugin
version, not per submission.

## The loop

```bash
while :; do
  reap
  dispatch
  if [ "$(count running)" -eq 0 ] && [ "$(count queue)" -eq 0 ]; then
    rm -rf lock
    if [ "$(count queue)" -eq 0 ]; then
      exit 0
    fi
    mkdir lock 2>/dev/null || exit 0
    echo $$ > lock/pid
    continue
  fi
  sleep 5
done
```

**reap** — for each entry in `running/`, is its pid still alive? If not, move it
to `done/` and drop the pid file. An empty pid file means the job never started
at all, which is equally over; leaving it in `running/` would hold its cores
for ever.

**dispatch** — if `paused` exists, do nothing. Otherwise walk `queue/` in number
order and start an entry only if the job count is under `slots`, its cores fit,
and its memory fits. Claiming is the `mv` into `running/`.

**exit** — the moment nothing is running and nothing is queued, the runner is
gone. The next submission starts it again. On a shared login node that matters:
there is no daemon of yours sitting there between batches.

## The five rules that make it safe

Each exists because the obvious version is wrong.

### Upload to `tmp/`, then move

`mv` within one filesystem is atomic, so the runner can never start a
half-uploaded script — which uploading straight into `queue/` would allow.

### Claim a job by moving it

Two runners racing for one entry cannot both win a `mv`, and `Move-Item` fails
outright when the destination exists, so nothing is ever dispatched twice.

### The dispatch number only ever climbs

It *is* the queue order. Derived from what the queue currently lists, it
restarted the moment a user cleared `done/` — their disk, and their right — and
the next job then sorted **ahead** of everything still waiting.

`sequence` holds the highest number ever issued; a claim takes one past that or
the queue, whichever is greater. Names are zero padded to four digits so a plain
`ls` reads in order, and both runners sort **numerically**, so passing 9999 —
where the padding runs out and `job_10000` sorts before `job_9999` — does not
invert the queue.

Two clients racing for a number can come away with the same one. That is a tie,
broken by the job id already in the name, not a job running out of turn.

### Nothing that is a record is overwritten or deleted

Entries move `queue/` → `running/` → `done/` and stay. `status/` is kept. Your
job directories, inputs, logs and outputs are never touched.

Exactly four things are removed, and none of them is a record:

| Removed | By | Why |
|---|---|---|
| `pids/<entry>` | the runner, on reap | the pid of a process that has ended |
| `lock/` | the runner, on exit | released, or reclaimed when its pid is dead |
| `paused` | you, by unticking | that is what unpausing is |
| `.moleditpy_rc` | the wrapper, at the start of a run | see below |

**The sentinel is cleared before the run, not after it.** A stale exit code left
in the job directory would otherwise be read as *this* run's outcome if this run
were killed before writing its own — reporting a fresh job as having finished
with the previous attempt's status. Clearing it first makes "no sentinel" mean
what it should: the wrapper never finished, so `LOST`.

It is removed before the `EXIT` trap is installed, so there is no window where a
stale value could be picked up. Nothing is lost by it either: the exit code is
already in the job list, and in `status/` for a queued job, and `.moleditpy_rc`
is a dotfile so it is never among the results fetched back.

This only ever matters when a wrapper is re-run by hand in a directory that has
already been used — every submission gets a directory of its own.

The runner script is named after a digest of its own contents, so an upgrade is
a **new file**. A runner already up is executing the old one, and bash reads a
script *by byte offset as it goes*: replace the contents underneath it and it
resumes in the middle of different text, with no warning.

Each job has its own directory, named with the job id as well as the timestamp.
The stamp is accurate only to the second, so two jobs of the same name submitted
within one second shared a directory — overwriting each other's wrapper and
inputs, and sharing a single `.moleditpy_rc`, so whichever finished first
decided what *both* were reported to have done.

### The runner re-checks the queue after releasing its lock

The delicate one. A job enqueued between "the queue is empty" and "the lock is
gone" would sit there with nobody left to run it: whoever enqueued it saw a live
runner, and so did not start one. Having released the lock the runner looks
again, and takes it back if something arrived.

**This is the entire reason exiting on an empty queue is safe.** It is also why
`ensure_runner` runs *after* the job is in the queue, never before: a runner
started first can empty the queue and exit before the job arrives.

A lock whose pid is dead is reclaimed — a runner killed with `SIGKILL`, or a
rebooted machine, would otherwise lock the host out of its own queue for ever.
`mkdir` is the lock rather than `flock` because it is atomic on NFS, which is
where a cluster home directory usually lives.

## Scheduling on resources

Two budgets, both defaulting to the machine's real capacity:

| Budget | Detected from | The job asks via |
|---|---|---|
| `cores` | `lscpu` → `sysctl hw.physicalcpu` → `/proc/cpuinfo` pairs | preset's *CPUs per task* |
| `memory` | `/proc/meminfo` → `sysctl hw.memsize` | preset's *Memory* |

An absent file means "ask the machine"; an explicit `0` means "do not schedule
on this at all". A job that asks for nothing waits for nothing.

**Physical cores, not hardware threads.** `nproc` and `ProcessorCount` count
logical processors, so a six-core machine claims twelve and two six-core jobs
land on six real cores. The `Detect` button in the host editor runs the same
detection the runner does, so the dialog and the queue never disagree.

**Cores alone are not enough.** Two jobs asking for 90 GB must not both start on
a 120 GB machine because the CPU happened to be free. Overcommitting CPU makes a
calculation slow; overcommitting memory gets it killed hours in, which is the
failure a user cannot recover from.

Dispatch is strict FIFO: an entry that does not fit stops the pass rather than
letting smaller ones past, which would starve anything asking for most of the
machine. A job asking for more than the machine has is clamped to the whole
machine, so it runs alone instead of waiting for ever.

The limits are re-read every pass, so changing them takes effect without
restarting anything — which is what **Apply limits now** relies on.

## Dependencies

A chained job carries `# moleditpy-after:` and `# moleditpy-require-success:`
rather than waiting on a pid in its wrapper, because the runner knows whether
the predecessor **succeeded** — which a wrapper watching a process cannot.

A job whose dependency can never be satisfied (the predecessor failed under
`require-success`, or was never queued here at all) is moved aside to `done/`
with `blocked` in `status/`. Left in the queue it would keep the runner alive
for ever, and a runner that exits when the queue empties must have no way to be
stuck with an immortal queue.

## Pausing

Creating `paused` stops the runner dispatching anything new. Jobs already
running are left alone — a pause that killed them would mean throwing away
however long they had been going.

The flag lives on the host, so it outlasts the dialog, the session, and the
runner's own comings and goings: a runner that exits and is started again by the
next submission finds the queue still held.

## How the plugin sees it

One command per poll lists `<dir> <entry>` for the whole host — the same
contract the queue-based schedulers meet with a single `squeue`.

| Where the entry is | State shown |
|---|---|
| `queue/` | PENDING |
| `running/` | RUNNING |
| not listed | ended — resolved by the sentinel sweep |
| `status/` says `blocked` | FAILED, with the reason |

A job that has left the queue is resolved by reading its wrapper's own
`.moleditpy_rc`, exactly as for every other backend. The exit code reported is
the wrapper's, never the runner's opinion of it.

## Two flavours

`remote_runner_ps.py` is the same queue in PowerShell, for a host with no POSIX
shell. It imports the constants, the entry format and the listing parser from
`remote_runner.py` rather than restating them, and `flavour_for(host)` picks
between them by **scheduler**, never by transport — the scheduler is what
decides the language of every script.

What PowerShell forces to be different:

- **`$pid` is taken.** It is an automatic variable holding *this* process's id,
  so a job's pid is never stored in it.
- **`&&` does not exist** in Windows PowerShell 5.1 — it is a parser error, not
  a no-op — so every sequence is `;` and every conditional is spelled out.
- **`Move-Item` fails when the destination exists**, which is what makes
  claiming safe there.
- **Liveness is `Get-Process`**, not `kill -0`.
- **`Set-Content -Encoding ascii`** everywhere: most encodings write a BOM on
  5.1, and a BOM in front of an exit code makes it unparseable.

A test asserts both modules expose the same set of builders. A Windows host
quietly losing one would leave its queue half-working with the suite still
green.

## What it is not

FIFO with two resource budgets and one-predecessor dependencies. No priorities,
no backfill, no fair share, no reservations, no accounting. It exists so a
workstation does not thrash, not to replace SLURM.

## Where the code is

| Module | Holds |
|---|---|
| `remote_runner.py` | the layout, the generated bash runner, every command |
| `remote_runner_ps.py` | the same queue for PowerShell |
| `runner.py` | `submit_to_runner`, `poll_runner`, `cancel_in_runner`, the limits |
| `tests/test_remote_runner.py` | the bash runner driven as real processes |
| `tests/test_remote_runner_ps.py` | the same, in PowerShell, Windows only |
| `tests/test_runner_mode.py` | that the plugin drives it in the right order |

Every claim about what the runner *does* is tested by running it. Text
assertions on generated shell have passed here before while the script was
semantically broken.
