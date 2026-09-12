# Security model

What this plugin holds, what it never holds, and what it does on your behalf on
a remote machine. For reporting a vulnerability, see [SECURITY.md](../SECURITY.md).

## The short version

* **No credential of yours is ever written to disk by this plugin.** Not
  passwords, not key material, not agent handles. (The one secret it does write
  is one it generates itself: the local API's token, and only if you switch
  that API on. See [`api_token`](#api_token-and-apijson).)
* **Keys stay where SSH already keeps them.** The plugin stores a *path* at
  most, and usually not even that — it lets your `~/.ssh/config`, your agent and
  your `known_hosts` do their jobs.
* **Host keys are never auto-accepted.** An unknown key stops the connection and
  produces an explicit prompt showing the fingerprint.
* **Everything it runs remotely is a script you can read before it is sent** —
  the Script preview tab is the exact bytes uploaded.

## What is stored, and where

Everything lives in `~/.moleditpy/job_manager/`, outside the plugin folder.

### `settings.json`

| Field | Example | Secret? |
|---|---|---|
| `hostname`, `username`, `port` | `login.hpc.example.org`, `alice`, `22` | no |
| `key_path` | `~/.ssh/id_ed25519` — a **path**, never the key | no |
| `jump_host` | `alice@bastion` | no |
| `remote_root` | `~/moleditpy_jobs` | no |
| `ssh_options` | `ServerAliveInterval=30` | no |
| `login_commands` | `module purge` | no |
| `ask_password` | `true` — a *flag* meaning "prompt me", not a password | no |
| presets | queue, walltime, modules, command template | no |
| `command_templates` | your saved command lines | no |
| `notify_webhook` | `https://hooks.slack.com/services/...` | **in effect, yes** |
| `notify_chat` | `true` — a *flag* meaning "use that webhook" | no |

The chat webhook is the one field here that acts as a credential: anyone
holding that URL can post to the room. It is stored in the clear, like the rest
of this file, because it is the only way to use it and because a webhook is
revoked in the workspace rather than here. Nothing is sent until you paste one
in, and what is sent is one sentence — the job's name, what became of it, and
the host it ran on. That sentence leaves your network; the input files, the
results and the paths do not.

`HostProfile` has **no password field at all**, so there is nothing for
`asdict()` to serialise even by accident. This is enforced by tests
(`tests/test_credentials.py::TestNoSecretIsPersisted`), which write a password
into the live session and then assert it appears in neither `settings.json`,
the host profile, nor any job record.

### `jobs.pmejbs`

Job records: name, host id, remote directory, queue id, state, exit code,
timestamps, the input paths you selected and the preset snapshot. No
credentials. It is global on purpose — HPC jobs outlive the open project.
Ordinary JSON inside; `.pmejbs` is MoleditPy's extension for a job list, the
same idea as `.pmeprj` for a project.

### `archived/jobs_<date>.pmejbs`

Clearing the table writes the current list here first rather than deleting it.
Same contents, same absence of credentials. Exports you make yourself (`.pmejbs`
or `.csv`) contain the same fields — including remote directory paths and
usernames, which is worth remembering before mailing one to anybody.

### `api_token` and `api.json`

Only written if you switch the local API on (**Extensions > Job Manager > Local
API...**); it is off by default and nothing listens until then.

`api_token` holds 32 random bytes, generated on first use and reused across
restarts, created with mode `0600` — the mode is set when the file is created,
not afterwards, so the secret is never briefly world-readable. On Windows the
file inherits the ACL of the directory, which is inside your own profile.

`api.json` is written while the API is listening and deleted when it stops. It
carries the port and the same token, and exists so a client can find the API
without being configured.

The token is a credential in the same sense the chat webhook is: **anything
running as you can read it**, and can then submit to your clusters exactly as
you could. That is the honest trust boundary — the API is a convenience for
programs you run, not a sandbox around them. The socket binds `127.0.0.1` and
the bind address is not configurable; a request carrying a browser `Origin` is
refused, so a web page cannot make your browser submit a job on your behalf.
*New token* in that window invalidates every client using the old one.

Full detail: [API.md](API.md).

### `cache/` and `relay/`

Working copies, both inside the data directory and created `0700`: a remote
file fetched to be looked at (rather than downloaded and kept), and an input
with its `[prevfile:...]` tags filled in on the way to the host.

They used to live under the system temp directory, which on a multi-user
machine is shared and where both names were predictable — so another user
could create the directory first, as a symlink, and decide where the write
landed, and read whatever arrived there. Neither is a secret, but both are
your data, and they belong where the rest of it is.

### Nowhere

The password for a paramiko host lives in a plain dict on the session's
`JobService` and dies with the process. It is not written, not logged, and not
put on a command line.

## How each backend authenticates

### OpenSSH backend (default)

Shells out to the `ssh` and `scp` you already have, which means it inherits
everything you have already configured: `~/.ssh/config`, agent keys,
`ProxyJump` bastions, per-host options. The plugin adds no key handling of its
own.

`BatchMode=yes` is always set. That is a deliberate security property, not just
ergonomics: a host that wants a password fails fast instead of blocking a worker
thread on a prompt nobody can see, and **no password can ever reach the process
table**. There is no `sshpass`, and no password is ever passed as an argument.

### paramiko backend (optional)

For hosts that need a password — which, given the choice, you should avoid
needing. `ssh-keygen -t ed25519` followed by `ssh-copy-id user@cluster` is a
one-off that removes the prompt, removes this backend, and removes any secret
from the plugin's memory entirely. The plugin says so wherever it offers to
take a password.

Notable behaviour:

* `~/.ssh/config` is consulted for `HostName`, `User`, `Port` and
  `IdentityFile`. Your host profile always wins; the config only fills in what
  you left blank.
* The agent and your default keys are used when no password is given
  (`allow_agent=True`, `look_for_keys=True`).
* **ProxyJump is refused, not ignored.** paramiko needs a real channel for it,
  and silently connecting *directly* to a host you told the plugin to reach
  through a bastion would violate the network path you asked for. Use the
  OpenSSH backend for jump hosts.
* Passwords are prompted on the GUI thread, masked
  (`QLineEdit.EchoMode.Password`), and cached for the session only. Removing a
  host forgets its password immediately.
* Polling never prompts. An uncached host simply fails its poll and backs off
  until you do something interactive.

## Host key verification

`RejectPolicy` — an unknown host key **stops the connection**; it is never
added silently. The Hosts dialog turns the resulting error into an explicit
confirmation, and only writes the fingerprint to `~/.ssh/known_hosts` after you
agree. When `~/.ssh/config` gives the host an alias, the fingerprint is filed
under the name the connection actually verifies, not under the alias.

**The prompt shows the key before you accept it** — the key type and the
`SHA256:…` fingerprint, the same string `ssh` and `ssh-keygen -l` print, so it
can be compared against the value your site publishes. The key is read first
and written only if you say yes; nothing is filed on the strength of a question
you could not check. (It is not the MD5 hex paramiko's `get_fingerprint()`
returns: OpenSSH stopped showing that in 6.8, so there would be nothing to
compare it against.)

A key that *changed* (rather than being unknown) is a different matter and is
not offered for trusting — that is the case where a warning is the point.

## What runs on the remote machine

Anything you type in the wizard runs on the cluster under your account. That is
the feature. The safeguards are:

* **The Script preview tab shows the exact script** that will be uploaded —
  directives, module loads, pre-commands, the payload, and the sentinel traps.
  Nothing is added afterwards.
* **Every path is quoted** before interpolation, in the shell that will
  actually read it — `remote_paths.quote` for a POSIX host (with a `~` left
  expandable) and `dialect.POWERSHELL.quote` for a Windows one, where a single
  quote is doubled and `$` never reaches an expression parser. Job names are
  reduced to `[A-Za-z0-9._-]`, so a name like `../../etc/passwd` becomes
  `etc_passwd` and `a;rm -rf /` becomes `a_rm_-rf`.
* **A file name cannot become part of the command.** `{input}` is substituted
  onto the command line as it stands — it must be, since a template is free to
  write `"{input}"` and quote it itself — so a file called `mol$(id).inp` would
  otherwise have run `id` on the host. A name containing anything a shell reads
  as syntax (`` ` ``, `$`, `;`, `&`, `|`, `<`, `>`, `(`, `)`, a quote, a
  backslash, a newline) is refused before the job is created, by the wizard, by
  the API (as a 400) and by the runner itself. Spaces and glob characters are
  allowed: neither can execute anything.
* **Command templates are yours.** The built-in ones are conventional
  invocations of well-known programs; a template you save is stored verbatim and
  is no more privileged than typing the command.
* **Cancel kills one process tree**, never a broad `pkill`: a process group on
  a POSIX host (`shell`), `taskkill /PID <id> /T` on Windows, or the queue's own
  `scancel`/`qdel`. A job id that is not a plain number is refused rather than
  interpolated, in both shells.

## Opening a job list from somewhere else

A `.pmejbs` file can come from a colleague, a backup or an email, so every field
in it is untrusted input the moment the user opens one. Two consequences are
handled explicitly:

* **The queue id is quoted** before it reaches a remote shell. It is
  interpolated into `scancel` / `qdel` / `kill`, so an id of
  `12345; rm -rf ~` in a crafted list would otherwise have been a command the
  user's own account ran on the cluster the moment they pressed Cancel.
* **The helper queue's entry is checked instead of quoted.** On a host running
  the built-in queue the id is a file name that the plugin builds a path from
  (`mv "queue/$entry"`), which quoting cannot protect, so it is required to
  match the shape the plugin itself writes — `job_0007_<job id>.sh` — and
  anything else is refused before a command is built. Both shells, and
  asserted for both in `tests/test_security.py`.
* **A job list carries no host details.** No hostname, username, key path or
  anything resembling a credential is in a job record, so opening one cannot
  add or alter a host profile — you can only ever act on hosts you configured
  yourself. Both are asserted in `tests/test_security.py`.

A job list still names remote directories and commands, and opening one as your
working list means the plugin will poll those jobs on *your* hosts. Treat an
unfamiliar file the way you would treat an unfamiliar script.

## Downloading results

File names in the remote directory listing are checked to be plain names before
being joined onto the local download directory. A compromised or hostile host
answering `../../.bashrc` to `ls` would otherwise have written outside the
download folder — the one place where a remote machine's output becomes a local
path.

## Trust boundaries

| You are trusting | Because |
|---|---|
| the remote host | you gave it a shell command to run |
| every program running as your user, if the local API is on | it can read the API token and submit as you |
| everyone on your tailnet, if you publish the web monitor | Tailscale's ACLs, not this plugin, decide who reaches the page |
| your `~/.ssh` config, keys and agent | both backends use them |
| the plugin's generated script | preview it before submitting |
| result files you download | they are handed to the host app's file openers, which is how the ORCA/Gaussian analyzers claim `.out` |

Downloaded results are opened through the application's own openers. A result
file is data from a machine you chose to trust; the plugin does not execute
anything it downloads.

## The web monitor

The Host Monitor can serve itself as a read-only page (**Web...** in its top
bar). Three properties define it, and all three have tests holding them:

* **It binds `127.0.0.1` only.** Not `0.0.0.0`, and not the Tailscale address
  either. `tests/test_web_monitor.py` proves it by *connecting* over this
  machine's routable address and requiring the connection to be refused —
  binding that address instead proves nothing, because under a `0.0.0.0`
  listener the bind still succeeds on Windows.
* **It is read-only.** There is no `do_POST`, `do_PUT`, `do_DELETE` or
  `do_PATCH` on the handler, and a test asserts none of them exists, so a later
  edit cannot quietly add a route that changes something.
* **It is off until asked.** The first time the window opens, nothing listens.
  The choice is remembered after that; closing the window releases the socket
  but keeps the preference.

Requests carry a random per-session token, in the link or in a cookie the first
load leaves behind (`HttpOnly`, `SameSite=Strict`). It is compared with
`secrets.compare_digest`. The token is **not** the API token — sharing that one
with a page you paste into a phone would hand a full-control credential to a
browser history.

Reaching the page from elsewhere is `tailscale serve --bg <port>`, which
requires HTTPS to be enabled for the tailnet (admin console ▸ DNS ▸ HTTPS
Certificates). The plugin checks that before running anything: without it
Serve has no certificate to obtain and simply waits, which is indistinguishable
from a hang. That is a
deliberate hand-off: exposure, TLS and identity are Tailscale's, governed by
your tailnet ACLs, and this process never binds a routable address or decides
whose certificate to trust. Anyone your ACLs let reach the machine can read
your host names, job names and job states — which is the whole content of the
page, and the reason it can do nothing else.

## Deliberate non-goals

* **No credential storage, no keyring integration.** Adding a place to save
  passwords would mean owning their protection; SSH already solved this with
  keys and agents.
* **No key generation or upload.** `ssh-keygen` and `ssh-copy-id` do it better.
* **No password for the OpenSSH backend.** Batch mode is what keeps secrets out
  of the process table.

## Reviewing this yourself

```bash
# Nothing secret in the settings file:
grep -ri "password" ~/.moleditpy/job_manager/settings.json

# The tests that hold the line:
python -m pytest tests/test_credentials.py -v
```
