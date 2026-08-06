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
   operations    │ schedulers: slurm · pbs · sge · shell         │  knowledge here
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
trap '__moleditpy_rc=$?; echo "$__moleditpy_rc" > .moleditpy_rc' EXIT
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

Both traps matter. The `EXIT` trap alone is not enough for a payload that calls
`exit` itself — it would never reach a trailing `echo`. The signal traps are
what stop a job the scheduler *kills* (walltime, preemption, `scancel`, node
drain) from reaching the `EXIT` trap with `$?` still 0 and being recorded as a
clean success.

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

## Persistence

Both files live in `~/.moleditpy/job_manager/`, **outside** the plugin folder:
the Plugin Installer replaces the whole package directory on update and carries
over only a file literally named `settings.json`, so anything kept beside the
code would be silently destroyed.

| File | Holds |
|---|---|
| `settings.json` | host profiles, submit presets, preferences, user command templates |
| `jobs.json` | tracked jobs — global on purpose, since HPC jobs outlive both the open project and the session |

Both are written atomically (temp file in the same directory + `os.replace`), so
a crash mid-write cannot leave truncated JSON. Unknown keys are ignored on load,
so a file written by a newer version does not break an older one.

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
| `tasks.py` | `BackgroundTask` / `run_async` on the shared pool |
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
| script execution | a real `bash` | the generated script's actual semantics |
| GUI | real PyQt6, offscreen | dialogs, poller timing, window lifetime |
| integration | the main app checked out as a sibling | the real `PluginContext` contract |

CI runs all four; the integration job fails deliberately if the real-context
tier skips, since a silently skipped tier is indistinguishable from a passing
one in the log.
