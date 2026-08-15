# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

`moleditpy_job_manager` is a MoleditPy plugin: submit calculations to a remote
machine over SSH (or to this one, with no SSH), track them, fetch the results.
Entry point is `job_manager/__init__.py::initialize(context)`, which registers
two menu entries under **Extensions > Job Manager**.

Job state lives in `~/.moleditpy/job_manager/` — *outside* the plugin folder,
because the Plugin Installer replaces the package directory wholesale on update
and carries over only a file literally named `settings.json`.

## Commands

```bash
python -m pytest tests/ -q -n auto            # the suite; ~100 s with xdist
python -m pytest tests/test_runner.py -q      # one file
ruff format job_manager tests && ruff check job_manager tests
```

`QT_QPA_PLATFORM=offscreen` is set by `tests/conftest.py`; the Qt modules skip
themselves where PyQt6 is absent (one CI job installs only pytest, on purpose).

Run the full suite and `ruff` before committing. CI runs six jobs — Python
3.11/3.12/3.13, a real-Qt GUI job, a Windows job, and an integration job that
clones the main app — so a change that passes only on this machine is not done.

## Layout

| Module | Role |
|---|---|
| `models.py` | `HostProfile`, `SubmitPreset`, `Job`. Pure Python, no Qt, no network |
| `store.py` | `settings.json` + `jobs.pmejbs`, both written atomically |
| `service.py` | The session object: submit, poll, download, cancel |
| `runner.py` | What actually happens on the host, per operation |
| `schedulers/` | One module per queue system; `base.py` builds the run script |
| `transport/` | `openssh`, `paramiko`, `local` — run a command, move a file |
| `remote_runner*.py` | The helper queue for hosts with no scheduler (bash / PowerShell) |
| `*_dialog.py` | Qt only. No logic that is worth testing lives here |

## Things that have bitten, and must not again

- **Every scheduler's script writes an exit-code sentinel from an EXIT trap**,
  to a temp name then `mv`. A trailing `echo` is skipped by a payload that
  calls `exit`, and `>` truncates before writing, so a poll landing mid-write
  reads an empty file and reports a finished job as LOST.
- **`ssh host 'cmd'` is neither a login nor an interactive shell.** No dotfile
  is read. A host's environment setup therefore goes *into the job script*, not
  merely around the submitting command — a queue runs that script later on a
  compute node where nothing of the submitting shell survives.
- **A bare program name resolves to whatever is on `PATH`.** On many Linux
  desktops `orca` is the GNOME screen reader. Docs tell users to give the full
  path; do not "helpfully" strip one.
- **`shutil.which("bash")` on Windows finds `System32\bash.exe`** — the WSL
  launcher, which cannot translate a Windows path. `transport/local.py` rejects
  it, and `tests/bash_support.py` validates a candidate by running a script
  through it before believing it. Do not reintroduce a bare `which("bash")`.
- **Build Windows path constants with explicit backslashes.** `os.path.join`
  uses `/` off Windows, which turns a comparison into a silent mismatch on the
  Linux runner while passing locally.
- **Two runner flavours must not drift.** `remote_runner.py` (bash) and
  `remote_runner_ps.py` (PowerShell) implement the same protocol; there are
  tests that compare them, and a change to one usually belongs in both.
- **Tests that drive a real shell take real time.** They are worth it — text
  assertions passed while a generated script was semantically broken — but keep
  poll intervals short and never let a test wait out a timeout on the happy
  path.

## Releasing

Version lives in one place: `PLUGIN_VERSION` in `job_manager/__init__.py`.

1. Bump it, run `ruff` and the full suite.
2. Commit, push to `main`, **wait for all six CI jobs to be green**.
3. `git tag -a vX.Y.Z -m "Version X.Y.Z"` and push the tag.
4. The Release workflow verifies the tag matches `PLUGIN_VERSION`, builds
   `job_manager_X.Y.Z.zip`, publishes it, and dispatches to
   `HiroYokoyama/moleditpy-plugins`, which updates the registry entry.

The dispatch step is `continue-on-error: true`: if `REGISTRY_PAT` is invalid it
401s and the release still reports success while the registry stays behind.
Check the registry entry after a release, and re-dispatch by hand if needed:

```bash
gh api repos/HiroYokoyama/moleditpy-plugins/dispatches \
  -f event_type=plugin_release \
  -f 'client_payload[repo]=HiroYokoyama/moleditpy_job_manager' \
  -f 'client_payload[tag]=vX.Y.Z'
```

Never tag or push without being asked to.

## Style

Comments explain *why*, and only where the reason is not obvious from the code
— most of the ones in this repo record a failure that has actually happened.
Do not add narration. Commit messages carry the failure scenario in prose.

## Privacy

Never copy a user's job data — molecule names, host names, directory paths —
into source, tests, documentation or commit messages. Examples use `myhost`,
`mycommand`, `/opt/...`. The job list at `~/.moleditpy/job_manager/` is the
user's working data: read it only when diagnosing, say so first, and keep what
you find out of the repository.
