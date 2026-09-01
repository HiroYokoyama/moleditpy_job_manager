# The local API

Another program on this machine can submit jobs, read their state and fetch
their results, through a small HTTP API the plugin serves on `127.0.0.1`.

It is **off until you switch it on**. Nothing listens on a machine where the
plugin is merely installed.

Everything it can do is something you could do in the wizard, and it does it
the same way: a submission over the socket builds the same preset object the
wizard builds and hands it to the same service, so it is polled, chained,
downloaded and announced exactly like one you typed in.

## Contents

* [Switching it on](#switching-it-on)
* [Finding it from a client](#finding-it-from-a-client)
* [The Python client](#the-python-client)
* [The command line](#the-command-line)
* [Routes](#routes)
* [Submitting](#submitting)
* [Errors](#errors)
* [Recipes](#recipes)
* [From inside MoleditPy](#from-inside-moleditpy)
* [Security](#security)

## Switching it on

**Extensions > Job Manager > Local API...**

Tick *Allow local programs to submit jobs*. The window then shows the URL and
the token a client needs, and the API starts listening immediately — and again
at every launch, until you untick it.

The port defaults to **8765**, which does not collide with the MCP Server
plugin's **7891** — the two can run side by side. A port already in use is not
an error either way: the API takes a free one instead, says which in the
window, and writes it where clients look, so nothing that discovers the API is
affected.

## Finding it from a client

While the API is listening it writes `~/.moleditpy/job_manager/api.json`:

```json
{
  "url": "http://127.0.0.1:8765/api/v1",
  "host": "127.0.0.1",
  "port": 8765,
  "token": "...",
  "api_version": 1,
  "pid": 12345,
  "started_at": 1786000000.0
}
```

That file is how a client finds the API without being configured. It is deleted
when the API stops, so its absence means "not running" rather than "wrong
port". The token itself lives in `api_token` beside it and survives restarts,
so a client configured once with a token keeps working.

Two environment variables skip discovery entirely, for a caller that already
knows: `MOLEDITPY_JOB_API_URL` and `MOLEDITPY_JOB_API_TOKEN`.

Every request must carry the token:

```
Authorization: Bearer <token>
```

`X-Job-Manager-Token: <token>` is accepted too, for a client where the
`Authorization` header is awkward to set.

## The Python client

`job_manager/api_client.py` is a client with no dependencies beyond the
standard library, and it imports nothing else from the plugin — copy the single
file next to your own program if that is easier than installing anything.

```python
from job_manager.api_client import JobManagerClient

client = JobManagerClient()          # discovers the running MoleditPy

job = client.submit(
    host="mycluster",
    files=["h2o.inp"],
    command="/opt/orca/orca {input} > {stem}.out",
    cpus_per_task=8,
    memory="16GB",
    fetch_globs=["*.out", "*.xyz"],
)

final = client.wait(job["id"])       # blocks until the queue is done with it
if final["state"] == "DONE":
    paths = client.download(final["id"], wait=True)["files"]
```

`wait()` asks MoleditPy, not the cluster, so a short interval here costs the
cluster nothing — the plugin still polls the queue on its own schedule.

## The command line

```bash
python -m job_manager.api_client ping
python -m job_manager.api_client hosts

python -m job_manager.api_client submit h2o.inp \
    --host mycluster \
    --command "/opt/orca/orca {input} > {stem}.out" \
    --cpus 8 --memory 16GB --fetch "*.out" --fetch "*.xyz"

python -m job_manager.api_client jobs --state ACTIVE
python -m job_manager.api_client log <job id> --lines 50
python -m job_manager.api_client wait <job id> --download
```

`wait` exits **0** when the job is `DONE` and **2** otherwise, so a shell script
can chain on it without reading the JSON. `--json` on any command prints the
raw reply instead of a summary.

And with nothing but `curl`:

```bash
TOKEN=$(python -c "import json;print(json.load(open('$HOME/.moleditpy/job_manager/api.json'))['token'])")

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/jobs
```

## Routes

Everything is under `/api/v1`. Replies are JSON objects.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/ping` | Version, and how many jobs and hosts there are |
| `GET` | `/hosts` | Configured hosts: id, name, target, scheduler, enabled |
| `GET` | `/presets` | Saved presets; `?host=` narrows to one host |
| `GET` | `/jobs` | Tracked jobs; `?state=`, `?host=`, `?name=`, `?limit=` |
| `POST` | `/jobs` | Submit a job — see below. `202` with the new record |
| `GET` | `/jobs/{id}` | One job |
| `DELETE` | `/jobs/{id}` | Stop tracking a finished job |
| `POST` | `/jobs/{id}/cancel` | Cancel it on the host |
| `POST` | `/jobs/{id}/download` | Fetch its results |
| `GET` | `/jobs/{id}/log` | Tail its log; `?lines=`, `?file=` |
| `GET` | `/jobs/{id}/files` | List its remote directory |

`?state=` takes a state name, or `ACTIVE` / `TERMINAL` for the whole group.

A job record is the stored record plus four fields the client would otherwise
have to derive: `active`, `terminal`, `elapsed_seconds`, `waiting_seconds`, and
`blocked_by` (the id of a predecessor that failed and left this one unable to
start).

`/jobs/{id}/log` and `/jobs/{id}/files` reach the host, so they answer when it
does — or `504` after 120 s.

`POST /jobs/{id}/download` starts a download and returns immediately. Send
`{"wait": true}` to have the reply held until the files are on disk, which is
what a script that then reads them wants; the `files` field is where they
landed. `into` picks the folder, `names` picks individual remote files.

## Submitting

`POST /api/v1/jobs`. `host` is required, and so is **one of** `command` or
`preset` — a submission with neither is refused rather than defaulting, because
the default command template names one particular program and running it on
somebody's input by accident is worse than an error.

```json
{
  "host": "mycluster",
  "name": "water opt",
  "files": ["/home/me/h2o.inp"],
  "command": "/opt/orca/orca {input} > {stem}.out",
  "cpus_per_task": 8,
  "memory": "16GB",
  "walltime": "24:00:00",
  "queue": "compute",
  "modules": ["orca/5.0.4"],
  "fetch_globs": ["*.out", "*.xyz"],
  "auto_download": true
}
```

| Field | Meaning |
|---|---|
| `host` | Host id, or its name as shown in the Hosts dialog |
| `files` | Local input files to upload; the first is what `{input}` names |
| `command` | The command line. `{input}`, `{stem}`, `{basename}` are substituted |
| `preset` | A saved preset on that host, by name or id, instead of the fields below |
| `name` | Job name; defaults to the input's filename |
| `queue`, `account`, `walltime` | Passed to the scheduler |
| `nodes`, `ntasks`, `cpus_per_task`, `memory` | The resource request |
| `modules`, `pre_commands`, `extra_directives` | Added to the generated script |
| `fetch_globs` | What to bring back |
| `auto_download` | Fetch the results automatically when it ends |
| `remote_dir` | Run in a directory already on the host, uploading nothing |
| `remote_input` | The input inside `remote_dir`, when there is one |
| `after_job` | Chain behind this job id, on the same host |
| `chain_any` | Chain on the predecessor *ending*, not on it succeeding |
| `start_after` | Hold until then: an epoch second, or `2026-01-31T18:30` |

Naming a `preset` and a field together is fine: the field wins, and the stored
preset is not modified.

The reply is `202` with the job record. Submission itself continues in the
background, so the record comes back `UPLOADING` and reaches the queue a moment
later — poll `GET /jobs/{id}`, or use the client's `wait()`.

`files` are read by MoleditPy off this machine's disk, so a relative path is
resolved against *MoleditPy's* working directory, not the caller's. The Python
client makes them absolute for you; anything else should send absolute paths.

## Errors

An error is a JSON object with the status and a sentence written to be shown to
a person as it is:

```json
{ "error": "No host called 'nowhere'. Known hosts: mycluster, mydesktop", "status": 404 }
```

| Status | When |
|---|---|
| `400` | The request is wrong: a missing field, a bad type, a file that is not there |
| `401` | No token, or the wrong one |
| `403` | The request carried a web page's `Origin` |
| `404` | No such route, host, preset or job |
| `405` | The right path, the wrong method |
| `409` | The request makes no sense here: a disabled host, a finished job to cancel, a download already running |
| `413` | The body is over 1 MB |
| `500` | A bug — the message is the exception, and the details are in MoleditPy's log |
| `503` | MoleditPy did not handle the request within 30 s |
| `504` | The host did not answer within 120 s |

## Recipes

### Submit one input and wait for the result

```python
from job_manager.api_client import JobManagerClient

client = JobManagerClient()

job = client.submit(
    host="mycluster",
    files=["/data/h2o.inp"],
    command="/opt/orca/orca {input} > {stem}.out",
    cpus_per_task=8,
    memory="16GB",
    fetch_globs=["*.out", "*.xyz"],
)

final = client.wait(job["id"])
if final["state"] != "DONE":
    raise SystemExit(f"{final['name']} {final['state']}: {final['last_error']}")

paths = client.download(final["id"], wait=True)["files"]
```

`auto_download` is on by default, so the results are usually already on disk by
the time `wait()` returns — `download(wait=True)` is what makes that certain
before the next line reads them.

### A batch, running one after another

```python
previous = None
for path in inputs:
    previous = client.submit(
        host="mycluster",
        files=[path],
        command="/opt/orca/orca {input} > {stem}.out",
        after_job=previous["id"] if previous else "",
        chain_any=True,          # carry on even if one of them fails
    )
```

Each job waits for the one before it, using the scheduler's own dependency
flag — the chain holds with MoleditPy closed. Without `chain_any`, a failure
stops everything behind it (and the monitor says so).

Submitting them all at once instead, with no `after_job`, lets the queue decide
the order; on a machine with no scheduler the plugin's own runner still limits
how many run together.

### Work already staged on the cluster

Nothing to upload — name the directory and the input inside it:

```python
client.submit(
    host="mycluster",
    remote_dir="/scratch/me/run042",
    remote_input="mol.inp",
    command="/opt/orca/orca {input} > {stem}.out",
)
```

### Watch a job while it runs

```python
job = client.job(job_id)
print(job["state"], job["elapsed_seconds"])
print(client.log(job_id, lines=40))       # the tail of its log
print(client.files(job_id))               # what is in the job directory
```

### From a shell, with no Python client

```bash
API=$HOME/.moleditpy/job_manager/api.json
URL=$(python  -c "import json,sys;print(json.load(open('$API'))['url'])")
TOKEN=$(python -c "import json,sys;print(json.load(open('$API'))['token'])")
AUTH="Authorization: Bearer $TOKEN"

JOB=$(curl -s -H "$AUTH" -H 'Content-Type: application/json'     -d '{"host":"mycluster","files":["/data/h2o.inp"],
         "command":"/opt/orca/orca {input} > {stem}.out"}'     "$URL/jobs" | python -c "import json,sys;print(json.load(sys.stdin)['job']['id'])")

curl -s -H "$AUTH" "$URL/jobs/$JOB"
```

Or let the shipped command line do it, which is the same thing with the
discovery and the polling already written:

```bash
python -m job_manager.api_client submit /data/h2o.inp     --host mycluster --command "/opt/orca/orca {input} > {stem}.out" --wait
```

### Handling failure

```python
from job_manager.api_client import JobApiError

try:
    job = client.submit(host="mycluster", files=[path], command=command)
except JobApiError as exc:
    # exc.status is the HTTP status; the message is written to be shown as it is.
    print(f"could not submit ({exc.status}): {exc}")
```

A job that ran and failed is not an error here — it comes back with
`state == "FAILED"`, `rc` set to the exit code, and `last_error` explaining what
the plugin saw.

## From inside MoleditPy

A plugin running in the same process should not go out over a socket to reach
the process it is already in. `job_manager.submit_job(request)` takes the same
dict as `POST /jobs` and returns the same job record, raising
`job_manager.api_core.ApiError` for anything it cannot serve. It must be called
on the GUI thread.

```python
import job_manager

job = job_manager.submit_job({
    "host": "mycluster",
    "files": [path],
    "command": "/opt/orca/orca {input} > {stem}.out",
})
```

`job_manager.submit_file(paths)` is the older handoff and does something
different on purpose: it opens the wizard *prefilled*, so the user chooses the
host and confirms. Use that when a person is present, and `submit_job` when one
is not.

## Security

See [SECURITY_MODEL.md](SECURITY_MODEL.md) for the plugin as a whole. What is
specific to the API:

* **It is off by default**, and starts only after you switch it on.
* **It binds `127.0.0.1` and nothing else.** The bind address is not
  configurable; there is no setting that puts this on a network.
* **Every request needs the token.** It is 32 random bytes, generated on first
  use and stored in `~/.moleditpy/job_manager/api_token`, mode `0600` on
  Unix — on Windows the file inherits the directory's ACL, which is the user's
  own profile directory.
* **Any program running as you can read that token**, and can then submit to
  your clusters exactly as you could. That is the actual trust boundary: this
  is a convenience for programs you run, not a sandbox around them.
* **A request carrying a browser `Origin` is refused.** A page on any site can
  make your browser POST to `127.0.0.1`; it cannot read the token or the reply,
  but a blind submission would still be a job submitted.
* **New token** in the Local API window invalidates every client using the old
  one — that is the revocation, and unticking the box is the off switch.
