# Architecture

Job Manager sits between the input generators and the result analyzers: it
uploads an input over SSH, submits it to a queue, watches the queue on a slow
timer, fetches the outputs when the job ends, and hands the result to whichever
plugin already claims that file type.

## Layers

```
                 ┌─────────────────────────────────────────────┐
   Qt dialogs    │ jobs_dialog   submit_dialog   hosts_dialog   │  GUI thread only
                 └───────────────────────┬─────────────────────┘
                                         │ signals
                 ┌───────────────────────▼─────────────────────┐
   coordination  │ JobService  ── owns ──▶ JobPoller (QTimer)   │  GUI thread,
                 │      │                        │              │  dispatches workers
                 └──────┼────────────────────────┼──────────────┘
                        │ worker threads         │ worker threads
                 ┌──────▼────────────────────────▼──────────────┐
   blocking      │ runner: submit / poll / fetch / cancel / tail │  no Qt, no network
   operations    │ schedulers: slurm·pbs·sge·shell·windows       │  knowledge here
                 └───────────────────────┬──────────────────────┘
                                         │
                 ┌───────────────────────▼──────────────────────┐
   transport     │ Transport ABC → OpenSSHTransport | Paramiko   │  the only code
                 └──────────────────────────────────────────────┘
                                         │
                 ┌───────────────────────▼──────────────────────┐
   persistence   │ JobStore → ~/.moleditpy/job_manager/*.json    │
                 └──────────────────────────────────────────────┘
```

### Why these seams

**`runner` and `schedulers` contain no Qt and no transport detail.** They take a
`Transport` and run synchronously, which is what lets the whole workflow be
tested against a fake transport with no event loop and no network.

**`Transport` is two methods and a file copy** (`run`, `upload`, `download`).
Everything above it — schedulers, poller, UI — talks only to that interface, so
a new backend is one file.

**The store is the single writer of job state, on the GUI thread.** Workers
return plain data through signals; nothing touches a widget off-thread.

**The service is not owned by any window.** It is created either by opening a
window or, at plugin load, by `_resume_tracking()` finding active jobs left
over from a previous session — because tracking that stops when MoleditPy is
restarted is not tracking. It then outlives every window: closing the monitor
must not stop polling. The status-bar counter is the only thing that says so
while no window is open, and it is installed alongside the service, once.

Cost when there is nothing to do: `_resume_tracking()` reads the job list — a
file the plugin would read anyway — and stops there if no job is active. No
service, no timer, no connection.

## Threading

| Runs on | What |
|---|---|
| GUI thread | every dialog, `JobService`, `JobPoller`, all store writes |
| `QThreadPool` (3) | submit, download, cancel, tail, test-connection |
| `QThreadPool` (2) | polling, at most one in flight per host |

Blocking calls (`Transport.run`, `upload`, `download`) must only ever be called
from a worker. Anything that needs to *ask* the user something — a password, a
confirmation — has to happen on the GUI thread **before** the worker is
dispatched, because a worker cannot open a dialog. That is why
`credentials.ensure_password()` is called from the dialogs and never from the
poller.

## Polling

* **One status call per host per cycle**, never one per job: a user with 40
  queued jobs still costs the login node a single `squeue`.
* **Slow by default** — 120 s, floor 5 s, and anything under 30 s shows a
  warning in the toolbar. A shared login node is not a status API.
* **The timer stops** when nothing is active and restarts on the next submit.
* **Errors back off** exponentially to 15 minutes, per host.
* **One poll in flight per host**, so a slow poll cannot stack up behind itself.
* Each poll opens a transport and **closes it in a `finally`** — without that,
  every cycle leaked a ControlMaster socket directory and a persistent `ssh`
  process for the life of the session.

## How a job's completion is detected

The generated run script writes the payload's exit code to `.moleditpy_rc`:

```bash
trap '__moleditpy_rc=$?; echo "$__moleditpy_rc" > .moleditpy_rc.tmp && mv -f .moleditpy_rc.tmp .moleditpy_rc' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP
```

A job that has disappeared from the queue is then resolved by reading that one
file, which avoids depending on `sacct` (frequently disabled) or on parsing
site-specific `qstat -f` output.

* **File present with 0** → `DONE`.
* **File present, non-zero** → `FAILED`, with the code shown in the table.
* **File absent** → `LOST`: the job left the queue without the wrapper
  finishing.

Written beside itself and renamed, in both languages, because the reading side
cannot tell an empty file from a missing one. `>` and `Set-Content` both
truncate before they write, and a poll landing in that window would read
nothing and report a finished job as `LOST`. A rename is one step.

Both traps matter. The `EXIT` trap alone is not enough for a payload that calls
`exit` itself — it would never reach a trailing `echo`. The signal traps are
what stop a job the scheduler *kills* (walltime, preemption, `scancel`, node
drain) from reaching the `EXIT` trap with `$?` still 0 and being recorded as a
clean success.

The Windows scheduler writes the same file from the same three states, in
PowerShell:

```powershell
try   { <payload>; $__moleditpy_rc = $LASTEXITCODE; $__moleditpy_done = $true }
catch { $__moleditpy_rc = 1 }
finally {
    if (-not $__moleditpy_done -and $null -ne $LASTEXITCODE) { $__moleditpy_rc = $LASTEXITCODE }
    Set-Content -Path .moleditpy_rc.tmp -Value $__moleditpy_rc -Encoding ascii
    Move-Item -LiteralPath .moleditpy_rc.tmp -Destination .moleditpy_rc -Force
}
```

`try/finally` is what `trap ... EXIT` is for, and the completion flag is what
`exit` inside the payload would otherwise defeat — without it the sentinel kept
its placeholder and a job that exited 0 was recorded as `FAILED`.

There is **no Windows equivalent of the signal traps**. `TerminateProcess`
stops the process dead and no `finally` runs, so a killed job writes nothing
and is classified `LOST` rather than `FAILED (rc=143)`. `-Encoding ascii` is
not incidental either: `Set-Content` writes a BOM with most encodings on
Windows PowerShell 5.1, and a BOM in front of the exit code makes it
unparseable.

## The helper queue on the host

A host with no scheduler has nothing deciding what runs when. Chained lanes
(`store.chain_lane_tail`) answer that without leaving anything behind, but the
order is fixed at submit time and they know nothing about cores or memory. The
helper is the other answer: a small script on the host holding a real FIFO
queue, in bash (`remote_runner.py`) or PowerShell (`remote_runner_ps.py`).

```
<remote_root>/.moleditpy_runner/
    lock/        single-instance lock (an atomic mkdir), holding pid
    queue/       job_0001_<id>.sh …  dispatched in name order
    running/     the script while its job runs
    pids/        the wrapper pid for each running job
    done/        the script once the job has ended
    status/      the exit code the runner observed
    tmp/         scripts being uploaded, before they are moved into queue/
    moleditpy_runner_<digest>.sh     the runner itself, one file per version
    slots cores memory paused runner.sha
```

**The queue is just numbered shell scripts.** Each is self-contained — it cds
into its job directory and runs the same wrapper every other backend uses, with
the same sentinel and the same signal traps — so completion is detected
identically everywhere, and the queue can be read, reordered or emptied over
plain `ssh` with `ls` and `mv`. Nothing needs this plugin to make sense of it.

Four rules make it safe, and each exists because the obvious version is wrong:

* **Uploaded to `tmp/`, then moved into `queue/`.** `mv` within a filesystem is
  atomic, so the runner can never start a half-uploaded script.
* **Sequence numbers are zero padded.** `ls | sort` puts `job_10` before
  `job_2`, so unpadded names dispatch in the wrong order past nine jobs. The
  job id in the name keeps two clients from colliding on one number.
* **A job is claimed by moving it out of `queue/`.** Two runners racing for one
  entry cannot both win a `mv` (or a `Move-Item`, which fails when the
  destination exists), so nothing is dispatched twice.
* **The runner re-checks the queue after releasing its lock.** A job enqueued
  between "the queue is empty" and "the lock is gone" would otherwise sit there
  with nobody to run it — whoever enqueued it saw a live runner and so did not
  start one. This is the whole reason exiting on an empty queue is safe.

**Nothing generated is ever overwritten or removed.** The runner script is named
after a digest of its own contents, so a new plugin version is a *new file*: a
runner already up is executing the old one, and **bash reads a script by byte
offset as it goes** — replace the contents underneath it and it resumes in the
middle of different text, with no warning. Queue entries move `queue/` →
`running/` → `done/` and stay; only the pid file of a finished job is removed,
which records nothing. Each job has its own directory named
`<stamp>_<name>_<job id>`; the id is there because the stamp is accurate only to
the second, and two jobs of the same name submitted within one second used to
share a directory — overwriting each other's wrapper and inputs, and sharing a
single `.moleditpy_rc`, so whichever finished first decided what *both* were
reported to have done.

### Scheduling on resources

Each queued script carries its request as comment headers the runner reads with
one `sed`: `# moleditpy-cores:`, `# moleditpy-memory:`, and the dependency tags.
They are comments, so the script still runs by hand.

A job is dispatched when its cores *and* its memory both fit within the budgets
in `cores` and `memory` — absent means "ask the machine", an explicit `0` means
"do not schedule on this at all". Cores alone are not enough: two 90 GB jobs on
a 120 GB machine must not both start because the CPU happened to be free.
Overcommitting CPU makes a calculation slow; overcommitting memory gets it
killed hours in.

**Physical cores, not hardware threads.** `nproc` and `ProcessorCount` count
logical processors, so a six-core machine claims twelve and two six-core jobs
land on six real cores. Both flavours count sockets × cores (`lscpu`, then
`sysctl`, then `/proc/cpuinfo` pairs; `Win32_Processor.NumberOfCores` on
Windows) and fall back to the logical count only if none of those answer. The
`Detect` button in the host editor runs the same detection, so the dialog and
the queue never disagree about the machine.

Dispatch is strict FIFO: a job that does not fit stops the pass rather than
letting smaller ones past, which would starve anything asking for most of the
machine. A job asking for more than the machine has is clamped to the whole
machine, so it runs alone instead of waiting for ever.

### Lifecycle and cost

The runner exits the moment its queue is empty; the next submission starts it
again. Nothing of yours sits on a shared login node between batches.

A submission to a host that already has the runner costs **five commands and
three transfers**: `mkdir` for the job directory, one setup call (prepare, the
three limits, and the digest of the runner script already there), one listing,
the enqueue `mv`, and `ensure_runner` — plus the input, the wrapper and the
queue entry. The runner script is a fourth transfer only when its digest
differs: `build_runner_script` is deterministic, so it is the same bytes for
every job on a host and changes only with the plugin version — one extra file
per upgrade, not per submission. Re-sending it was an `scp` per job. The digest
is believed only when the file it names is still there, or a deleted script
would be skipped and then started. That matters most on Windows, where OpenSSH cannot
multiplex and every round trip is a full handshake.

### Two flavours, one vocabulary

`remote_runner_ps.py` imports the constants, the entry format and the listing
parser from `remote_runner.py` rather than restating them, so bash and
PowerShell cannot drift apart on what an entry is called or what a header
means. `flavour_for(host)` picks between them by **scheduler**, never by
transport — the scheduler is what decides the language of every script.

A test asserts both modules expose the same set of builders; a Windows host
losing one of them would otherwise leave its queue half-working with the suite
still green.

## State machine

```
NEW → UPLOADING → SUBMITTED → PENDING → RUNNING → COMPLETING ─┬→ DONE
                                                              ├→ FAILED
        (cancel) ─────────────────────────────────────────────┼→ CANCELLED
        (vanished, no sentinel) ──────────────────────────────┴→ LOST
                                          DONE/FAILED → DOWNLOADING → back
```

`ACTIVE_STATES` (`SUBMITTED`, `PENDING`, `RUNNING`, `COMPLETING`) are the ones
the poller still contacts the host for. `TERMINAL_STATES` never change again on
their own.

`QUEUED` and `BLOCKED` are **display states**: they are never stored and never
returned by a scheduler. Both describe a job the queue calls PENDING, and they
are derived on the way to the screen because the distinction is the user's, not
the queue's — waiting its turn, or waiting for something that already failed.
`store.chain_blocker()` decides, and it answers None for schedulers whose
dependency releases on the predecessor ending (`chain_releases_on_failure`) and
for jobs submitted with `chain_any`.

It walks the **whole chain**, not just the job in front. In A(failed) ← B ← C,
C is as dead as B: B never starts, so it never ends, so nothing behind it is
ever released. Only B used to count as blocked — which cost more than a wrong
label, since C then counted as a live lane and held one of the host's slots for
the rest of the session. `chain_any` is read at the link that meets the
failure rather than on the job being asked about, because a loose dependency
releases on a predecessor that *ended* badly and not on one that never ran.

## Persistence

Both files live in `~/.moleditpy/job_manager/`, **outside** the plugin folder:
the Plugin Installer replaces the whole package directory on update and carries
over only a file literally named `settings.json`, so anything kept beside the
code would be silently destroyed.

| File | Holds |
|---|---|
| `settings.json` | host profiles, submit presets, preferences, user command templates |
| `jobs.pmejbs` | tracked jobs — global on purpose, since HPC jobs outlive both the open project and the session |
| `archived/jobs_<date>.pmejbs` | lists written out by **Clear List**, never deleted automatically |
| `downloads/` | fetched results, one directory per job |

`.pmejbs` is MoleditPy's extension for a job list (`.pmeprj` is a project); the
contents are ordinary JSON. A `jobs.json` written before the extension existed
is still read, and the next save migrates it.

Where a job list *sits* decides how it opens: a file inside `archived/` is
history and is shown read-only, while one from anywhere else is merged into the
live table. That is the only difference between the two paths.

Both are written atomically (temp file in the same directory + `os.replace`), so
a crash mid-write cannot leave truncated JSON. Unknown keys are ignored on load,
so a file written by a newer version does not break an older one.

An atomic *write* is not an atomic read-modify-write, and two MoleditPy windows
share these files. Each holds the whole job list in memory, so the second to
save would write its own view straight over the first's and the first window's
jobs would simply be gone — along with the remote directory that is often the
only way back to results still on the cluster. `save_jobs()` therefore re-reads
the file and keeps any job this session has never heard of. Ours win wherever
both know a job, so a save can never overwrite a state just observed, and ids
removed on purpose are remembered so that keeping unknown jobs cannot undo a
deletion.

Downloads are staged: each file arrives as `<name>.moleditpy-part` and is
renamed once the transfer completes. Results land in the directory the user is
working in, so a transfer cut off half way would otherwise leave a truncated
`.out` there under its real name, on top of the previous good copy.

## Module map

| Module | Responsibility |
|---|---|
| `__init__.py` | plugin entry point, menu actions, the public `submit_file()` handoff |
| `models.py` | `HostProfile`, `SubmitPreset`, `Job`, canonical states. Pure stdlib |
| `store.py` | JSON persistence, preferences, pruning |
| `service.py` | session-scoped coordinator; owns the store, poller and passwords |
| `poller.py` | the timer, per-host backoff and the in-flight guard |
| `runner.py` | submit / poll / fetch / cancel / tail, blocking |
| `schedulers/` | per-queue directives, submit verb, status parsing |
| `transport/` | `Transport` ABC, OpenSSH and paramiko backends |
| `credentials.py` | the password prompt, GUI thread only |
| `command_templates.py` | built-in command lines per program |
| `remote_paths.py` | POSIX path building and shell quoting |
| `dialect.py` | the non-job commands (mkdir, sentinel read, list, tail), per shell |
| `remote_runner.py` | the queue on the host: layout, generated script, commands |
| `remote_runner_ps.py` | the same queue, for a host with no POSIX shell |
| `tasks.py` | `BackgroundTask` / `run_async` on the shared pool |
| `status_widget.py` | the job counter in the host's status bar |
| `taskbar.py` | the same count on the application icon (Dock / task bar / launcher) |
| `notify.py` | the desktop notification raised when a job ends |
| `input_scan.py` | the memory and core request stated in an input file |
| `*_dialog.py` | the three windows |

## The `submit_file()` handoff

Other plugins hand a freshly written input straight to the wizard:

```python
job_manager.submit_file(paths, name="")   # → True if the wizard opened
```

They find this plugin through the host's plugin list rather than importing it
(plugins are not importable by name from each other) and identify it by this
attribute, so a plugin that merely shares the name but predates the API is
correctly treated as absent. ORCA and Gaussian Input Generator Pro use it for
their **Submit to Cluster...** buttons; see `cluster_link.py` in either repo for
the ~40-line pattern. The name and signature are a contract and will not change
without a major version.

## Testing

| Tier | What it needs | What it proves |
|---|---|---|
| unit | pytest only | models, store, schedulers, runner, transports (fakes) |
| script execution | a real `bash`, a real PowerShell | the generated scripts' actual semantics, including the runners as live processes |
| GUI | real PyQt6, offscreen | dialogs, poller timing, window lifetime |
| integration | the main app checked out as a sibling | the real `PluginContext` contract |

CI runs all four; the integration job fails deliberately if the real-context
tier skips, since a silently skipped tier is indistinguishable from a passing
one in the log.
