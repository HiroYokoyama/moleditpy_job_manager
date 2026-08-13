# Security model

What this plugin holds, what it never holds, and what it does on your behalf on
a remote machine. For reporting a vulnerability, see [SECURITY.md](../SECURITY.md).

## The short version

* **No secret is ever written to disk by this plugin.** Not passwords, not key
  material, not agent handles.
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

For hosts that need a password. Notable behaviour:

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
| your `~/.ssh` config, keys and agent | both backends use them |
| the plugin's generated script | preview it before submitting |
| result files you download | they are handed to the host app's file openers, which is how the ORCA/Gaussian analyzers claim `.out` |

Downloaded results are opened through the application's own openers. A result
file is data from a machine you chose to trust; the plugin does not execute
anything it downloads.

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
