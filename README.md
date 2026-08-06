# Job Manager

[![Tests](https://github.com/HiroYokoyama/moleditpy_job_manager/actions/workflows/tests.yml/badge.svg)](https://github.com/HiroYokoyama/moleditpy_job_manager/actions/workflows/tests.yml)
![Coverage](https://img.shields.io/badge/coverage-%3E90%25-brightgreen)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [MoleditPy](https://github.com/HiroYokoyama/python_molecular_editor) plugin that
runs your calculations on a remote cluster without leaving the editor.

MoleditPy can already *write* QM inputs (ORCA / Gaussian / VASP / QE / CP2K Input
Generator Pro) and *read* the results (ORCA Result Analyzer, Gaussian MO & Freq
Analyzers, Cube Viewer). Job Manager fills in the middle: upload, submit, watch,
fetch, open.

## What it does

- **Submit** an input file to SLURM, PBS/Torque, SGE/UGE — or to a plain
  background process on a machine with no queue at all.
- **Track** every job in one table: queue id, state, elapsed time. Status
  survives closing the window, closing the project, and restarting MoleditPy.
- **Fetch** the outputs automatically when a job ends, then hand the result to
  whichever plugin already claims that file type — a finished ORCA run opens in
  the ORCA Result Analyzer with no extra configuration.
- **Tail** the remote log, or cancel a job, from the same window.

## Requirements

- MoleditPy 4.x
- An SSH client. `ssh` and `scp` ship with Windows 10+, macOS and Linux, and the
  default backend uses them directly — **no pip packages required**.
- Optional: `pip install paramiko`, only if a host needs password authentication.

## Two SSH backends

| | OpenSSH (default) | paramiko (optional) |
|---|---|---|
| Install | nothing | `pip install paramiko` |
| Auth | keys and ssh-agent | keys, agent **and passwords** |
| `~/.ssh/config`, `ProxyJump` | inherited automatically | not read |
| Connection | one process per command (multiplexed on macOS/Linux) | one persistent session |

The OpenSSH backend runs `ssh` in batch mode on purpose: a background thread must
never block on an invisible password prompt. If a host only accepts passwords,
switch it to the paramiko backend.

**Passwords are never written to disk.** They are held in memory for the session
and asked for again after a restart. Unknown host keys are rejected, not
silently trusted — you are shown the fingerprint and asked.

## Polling is deliberately slow

A login node is not a status API, so:

- one `squeue`/`qstat` per **host** per cycle, no matter how many jobs you have;
- 120 s by default, and the interval cannot be set below 30 s;
- the timer stops entirely when no job is active;
- a host that errors backs off exponentially, up to 15 minutes;
- **Refresh Now** is there when you actually need an answer immediately.

## How completion is detected

Every generated script ends with:

```bash
your_command
__moleditpy_rc=$?
echo "$__moleditpy_rc" > .moleditpy_rc
exit $__moleditpy_rc
```

When a job disappears from the queue, the plugin reads that one file: an exit
code means finished (0 → DONE, anything else → FAILED with the code shown);
no file means the job was killed before the command returned. This works
identically on all four schedulers and needs neither `sacct` (often disabled) nor
site-specific `qstat -f` parsing.

## Usage

1. **Plugins ▸ Job Manager ▸ Job Monitor**, then **Hosts…** to add a cluster:
   hostname, user, scheduler, and a remote working directory. Press
   **Test Connection**.
2. **New Job…** — pick the host, add your input file, fill in walltime / nodes /
   memory / modules and the command to run (`orca {input} > {stem}.out`), check
   the **Script preview** tab, and submit. Save the settings as a named preset to
   reuse them.
3. Watch the table. When the job finishes its results are downloaded and the main
   output is opened for you.

Command template placeholders: `{input}`, `{stem}`, `{basename}`, `{nodes}`,
`{ntasks}`, `{cpus}`, `{memory}`, `{walltime}`, `{queue}`.

## Where your data lives

`~/.moleditpy/job_manager/`

- `settings.json` — host profiles, submit presets, preferences
- `jobs.json` — the tracked jobs
- `downloads/` — fetched results, one directory per job

Deliberately **outside** the plugin folder: the Plugin Installer replaces that
folder wholesale on update, so anything stored there would be lost. Jobs run for
days; the record of them has to outlive an update.

## Development

```bash
python -m pytest tests/ -v --cov=job_manager --cov-report=term-missing
```

The suite is fully offline — a fake transport records the commands that would
have been sent, so submit → poll → complete → download is exercised end to end
without a cluster. GUI tests need PyQt6 and skip themselves without it, which is
what lets the whole suite run on a bare `pip install pytest`.

## License

MIT — see [LICENSE](LICENSE).
