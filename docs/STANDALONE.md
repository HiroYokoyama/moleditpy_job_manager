# Job Manager without MoleditPy

Job Manager does not need the molecular editor. It submits, tracks and fetches
any command line — ORCA, Gaussian, CP2K, xTB, a shell script of your own — and
the whole of it runs as a desktop application of its own. MoleditPy adds one
thing on top: opening a result in the editor.

## Contents

* [Installing](#installing)
* [Starting it](#starting-it)
* [The command line](#the-command-line)
* [Agents and scripts](#agents-and-scripts)
* [Where your data lives](#where-your-data-lives)
* [Alongside the MoleditPy plugin](#alongside-the-moleditpy-plugin)
* [What needs MoleditPy](#what-needs-moleditpy)
* [Updating and removing](#updating-and-removing)

## Installing

```bash
pip install moleditpy-job-manager
```

Python 3.10 or later. This brings PySide6-Essentials, which is all the window
needs. Two optional backends are extras:

| Install | Adds |
|---|---|
| `pip install "moleditpy-job-manager[ssh]"` | paramiko: one SSH session kept open, password login |
| `pip install "moleditpy-job-manager[job-server]"` | cryptography: authenticated [Job Server](JOB_SERVER.md) destinations |
| `pip install "moleditpy-job-manager[all]"` | both |

OpenSSH destinations need no extra: the system `ssh` client is used, as in the
plugin. A virtual environment keeps Job Manager's Qt apart from anything else
on the machine:

```bash
python -m venv ~/.venvs/job-manager
~/.venvs/job-manager/bin/pip install moleditpy-job-manager
```

Without pip, the release zip works too: unpack it and run
`python -m job_manager` from the folder that contains `job_manager/`.

## Starting it

```bash
moleditpy-job-manager                  # the Job Monitor
moleditpy-job-manager --host-monitor   # the Host Monitor
```

`python -m job_manager` does the same. A second launch brings the running Job
Manager to the front instead of starting another tracker.

Everything from [the standard workflow](WORKFLOW.md) applies unchanged: add a
host, submit, watch the queue, fetch results. Where that guide names the menu
**Extensions > Job Manager**, use the buttons in the Job Monitor's own window;
the Local API switch, for instance, is under **Settings... > Local API...**.

## The command line

The package installs a second command, `moleditpy-job`, which is the client in
[the local API guide](API.md#the-command-line) under a shorter name:

```bash
moleditpy-job ping
moleditpy-job hosts
moleditpy-job submit h2o.inp --host myhost \
    --command "/opt/orca/orca {input} > {stem}.out" \
    --cpus 4 --memory 16GB --wait
moleditpy-job jobs --state ACTIVE
```

It talks to a running Job Manager, so start the window and switch the API on
once (**Settings... > Local API...**); it is served again at every launch until
you switch it off. `python -m job_manager.api_client` is the same program.

## Agents and scripts

`submit --wait` submits, waits for the queue and downloads the results in one
call, and exits **0** when the job is `DONE` and **2** otherwise, so an agent
working through a shell can branch on the exit code alone. `--json` on any
command prints the full reply for an agent that wants to read it.

For Python, `JobManagerClient` is in the same package:

```python
from job_manager.api_client import JobManagerClient

client = JobManagerClient()
job = client.submit(host="myhost", files=["h2o.inp"],
                    command="/opt/orca/orca {input} > {stem}.out")
final = client.wait(job["id"])
```

The API listens on `127.0.0.1` only, so a client always runs on the machine
where Job Manager is running. A client that must not depend on Qt — an agent in
its own minimal environment, say — can copy `job_manager/api_client.py` on its
own: it uses nothing but the standard library. See [the local API](API.md) for
discovery, tokens and every route.

## Where your data lives

The same place as the plugin's: `~/.moleditpy/job_manager/` — hosts, presets,
the job list, the API token. Set `MOLEDITPY_JOB_MANAGER_DIR` to use another
folder, for a separate set of hosts and jobs or for trying a release out
without touching your real list:

```bash
MOLEDITPY_JOB_MANAGER_DIR=~/job-manager-test moleditpy-job-manager
```

## Alongside the MoleditPy plugin

The pip package and the plugin can be installed together without stepping on
each other's files. MoleditPy loads the plugin from its plugin folder by path,
and the plugin's own background process starts from that folder too, so
neither picks up the pip copy; the `moleditpy-job-manager` and `moleditpy-job`
commands run the pip copy.

What they do share is the data folder. That is the point — one list of hosts
and jobs, whichever way you open it — but it has two consequences:

* **Only one Job Manager tracks at a time.** Whichever starts first does the
  polling; the second brings the first to the front.
* **Keep the two at the same version.** Both are released from the same tag;
  when you update one through the Plugin Installer, update the other with pip,
  so an older copy never reads a job list a newer one wrote.

## What needs MoleditPy

Only **Open in MoleditPy** on a result file, which hands the file to the
editor. Reading output in the built-in viewer, tailing logs, downloading and
the Host Monitor all work as they do in the plugin.

## Updating and removing

```bash
pip install --upgrade moleditpy-job-manager
pip uninstall moleditpy-job-manager
```

Uninstalling leaves `~/.moleditpy/job_manager/` in place, as removing the
plugin does; delete that folder yourself if you want the hosts and job list
gone as well.
