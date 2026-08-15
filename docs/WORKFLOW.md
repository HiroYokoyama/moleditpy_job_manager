# Standard workflow

The normal path, end to end, plus what to do when a job goes wrong.

## 1. Add a host, once

**Extensions → Job Manager → Job Monitor → Hosts...**

| Field | Notes |
|---|---|
| Hostname / Username / Port | Or just the alias from your `~/.ssh/config` |
| Backend | **OpenSSH** unless the host needs a password, or **This machine** for no SSH at all |
| Scheduler | SLURM, PBS/Torque, SGE/UGE, Built-in (background process), or Built-in (Windows, PowerShell) |
| Private key | Optional. Leave empty to use your agent and `ssh_config` |
| Jump host | `user@bastion` — OpenSSH backend only |
| Remote root | Where job directories are created, default `~/moleditpy_jobs` |
| Environment | Read `/etc/profile` and your `~/.bash_profile`, `~/.profile`, `~/.bashrc` first. On for a new host |
| Login commands | Run after those, e.g. `module load orca` |

> **"But it works when I ssh in myself."** `ssh host command` gets a shell that
> is neither a login shell nor an interactive one, so **none of your dotfiles is
> read** — while logging in by hand reads all of them. A program installed under
> your home directory, or provided by `module`, is therefore simply not on
> `PATH` for the job. That is what **Environment** is for, and it applies both to
> the commands sent to the host and to the top of every job script, because
> SLURM and PBS run that script later on a compute node where nothing of the
> submitting shell survives.
>
> Two things it cannot fix. Debian's stock `~/.bashrc` returns immediately for a
> non-interactive shell, so keep module loads *above* that guard or name them in
> **Login commands**. And a bare program name is still resolved by whatever is on
> `PATH`: on most Linux desktops `orca` is the GNOME **screen reader**, not the
> quantum chemistry package, and a job calling it prints "Cannot start the screen
> reader because it cannot connect to the Desktop" and exits 1. Give the full
> path — `/opt/orca6/orca` — which ORCA requires anyway for `%pal nprocs > 1`.

> **Set up a key rather than a password.** It is less work, not more:
> `ssh-keygen -t ed25519` then `ssh-copy-id user@cluster`, once, on this
> machine. After that the default OpenSSH backend connects with no prompt and
> no extra package — and most clusters expect keys anyway, with many refusing
> password logins outright. The paramiko backend exists for the hosts where
> that is not an option.

Press **Test Connection**. It round-trips `echo` + `hostname` and reports the
remote name. An unknown host key stops here and offers to show you the
fingerprint — see [SECURITY_MODEL.md](SECURITY_MODEL.md).

## 2. Write an input

Any MoleditPy input generator. ORCA Input Generator Pro and Gaussian Input
Generator Pro have a **Submit to Cluster...** button next to Save that hands the
file straight to the wizard, prefilled — no file picker, no retyping the name.

Everything else: **drop the input file onto the Job Monitor** and the wizard
opens with it already added, named after it, and with the right command
template chosen where the extension says so unambiguously. You can also drop
files straight onto the wizard, or add them with **Add files...**.

Dropping onto the *main* MoleditPy window is not the same thing — input
extensions are deliberately not claimed application-wide, since that would take
`.inp` and `.xyz` away from simply being opened.

## 3. Submit

**New Job...** collects four things:

1. **Host and preset.** A preset is a reusable resource request for one host.
2. **Input files.** The *first* one is what `[input]` expands to; the rest ride
   along into the same remote directory (basis sets, restart files, POTCAR...).
3. **Resources** — queue, walltime, nodes, tasks, CPUs, memory, modules,
   pre-commands, extra directives.
4. **The command.** Pick one from the **Template...** dropdown beside the field:

   | Program | Template |
   |---|---|
   | ORCA | `$(which orca) {input} > {stem}.out` |
   | Gaussian 16 | `g16 {input}` |
   | Gaussian 09 | `g09 {input}` |
   | CP2K | `mpirun -np {ntasks} cp2k.psmp -i {input} -o {stem}.out` |
   | GAMESS (US) | `rungms {input} 00 {ntasks} > {stem}.log` |
   | MOPAC | `mopac {input}` |
   | NWChem | `mpirun -np {ntasks} nwchem {input} > {stem}.out` |
   | Psi4 | `psi4 -i {input} -o {stem}.out -n {cpus}` |
   | PySCF | `python {input} > {stem}.out` |
   | Quantum ESPRESSO | `mpirun -np {ntasks} pw.x -in {input} > {stem}.out` |
   | VASP | `mpirun -np {ntasks} vasp_std > vasp.out` |
   | xTB | `xtb {input} --opt > {stem}.out` |

   The list puts the likely program first for the input you selected, and fills
   an empty command automatically when the extension is unambiguous. `.inp` is
   left alone — ORCA, CP2K and GAMESS all use it.

   `srun` works in place of `mpirun -np {ntasks}` on most SLURM sites.

Check the **Script preview** tab: that is the exact script that gets uploaded.
Then **Submit**.

### What the wizard remembers

Everything that describes the *site* rather than the molecule comes back the
next time you submit to the same host: queue, account, walltime, nodes, tasks,
modules, pre-commands, extra directives, the command, the fetch patterns and
whether results download by themselves. No preset needs naming for this — a
preset is the deliberate, named version of the same idea, and one you have
chosen always wins over what was merely used last.

**Cores and memory are the exception.** They describe the molecule, and with
**Take these two from the input file** ticked they are read from the input each
time, so last week's twelve cores are not carried onto a job that does not need
them. Untick it and they are remembered like everything else.

### Where the results go

Beside the input file, by default — the directory you are already working in.
Untick **...next to the input file** and they go to one shared download folder
instead; a job with no local input to sit beside uses that folder either way,
as does one whose input directory has since gone.

An input file is never overwritten by a downloaded result of the same name, so
a fetch pattern of `*.xyz` against an input called `mol.xyz` leaves your file
alone.

**The wrapper's own `job.log` is not downloaded.** It holds whatever the command
wrote to stdout and stderr, while the calculation's real output is the file the
command was told to write — and `*.log` in the default patterns is there for
Gaussian's output, which is a different file. It stays on the host, where **Tail
Log** reads it live. Name it exactly in the fetch patterns, or clear the patterns
altogether, and it comes back like anything else.

**A job that failed keeps its log**, because then it is usually the only
evidence there is — it holds the stderr nothing else recorded. So does a
download asked for while the job is still running, which is normally asked for
to read exactly that.

### Placeholders

Both spellings work, so you never have to fight the shell:

| Tag | Expands to |
|---|---|
| `{input}` / `[input]` | the first input file's name |
| `{basename}` | the same thing, spelled differently |
| `{stem}` / `[stem]` | that name without its extension |
| `{output}` / `[output]` | `<stem>.out` |
| `{name}` / `[name]` | the job name, reduced to safe characters (`opt run` → `opt_run`) |
| `{jobdir}` / `[jobdir]` | the directory on the host the job runs in |
| `{ntasks}` `{nodes}` `{cpus}` | the resource fields |
| `{cpus_per_task}` | a longer spelling of `{cpus}` |
| `{memory}` `{walltime}` `{queue}` | likewise |

Unknown tags are left verbatim, and shell syntax that merely looks like a tag —
`awk '{print $1}'`, `if [ -f x ]` — is passed through untouched. Nothing is
quoted for you, so quote the tag yourself where the value may contain spaces:
`cd "{jobdir}"`.

A command that names an input — `{input}`, `{basename}`, `{stem}`, `{output}` —
is refused for a job that has none, rather than submitted as `orca  > .out` for
you to find in tomorrow's log.

### Work that is already on the host

Files staged on the cluster days ago — generated there, copied with `rsync`,
left over from a previous run — do not need uploading again. Tick **Work
already on the host** and give the directory:

| Field | Meaning |
|---|---|
| **Directory** | where the job runs. `~/runs/mol42` or an absolute path. It must already exist |
| **Input file there** | optional; a file in that directory, which `{input}` and `{stem}` then stand for |

**Check** asks the host what is in the directory before you commit to it, and
says so if the input file you named is not among them.

Input files stay optional throughout. With none, this is a **command-only job**:
whatever you type in **Command** is what runs, over what is there. Any files you
*do* list are uploaded into that same directory alongside it.

Two things differ from an ordinary job, both because the directory is yours
rather than the plugin's:

- **It is checked, not created.** A directory that is not there is an error at
  submit time, rather than a `mkdir -p` of the typo and a job that runs in an
  empty directory.
- **Everything the wrapper writes carries the job id** — `moleditpy_<id>.sh`,
  `moleditpy_<id>.log`, `.moleditpy_rc_<id>`. So you can submit several jobs
  into one prepared directory without them overwriting each other's exit code,
  which is the one collision that would make a finished job report the wrong
  outcome.

Results are fetched from that directory by the usual patterns, and downloaded
to the shared download folder — there is no local input for them to sit beside.
Nothing there is ever deleted or renamed — but a file you *upload* does replace
one of the same name, which is the one way a prepared directory can lose
something.

Submitting with no input files *and* no directory is allowed as well, and asks
first: the command then runs in a new empty directory, which is occasionally
what you want and much more often a file you forgot to add.

### Saving your own template

Type a command, then **Template... → Save current command as...** and give it a
name. It is stored in `settings.json` next to your hosts and presets, appears in
the same dropdown from then on, and can be removed with **Delete a saved
template...**.

### Running on this machine

Pick **This machine (no SSH)** as the backend and the same workflow runs
locally: no hostname, no keys, no network. *Remote root* becomes an ordinary
directory here, and "upload" and "download" are file copies.

The bash schedulers need a POSIX shell — free on macOS and Linux, and on
Windows meaning Git Bash or WSL. The Hosts dialog says so if it cannot find
one. On Windows you can instead pick the **Built-in (Windows, PowerShell)**
scheduler and need nothing at all; see below.

### Running jobs one after another

Tick **Run after the job already queued on this host** and the job is held until
the previous one has finished. The wizard names the job it will follow and how
it will be held.

Each scheduler is told in its own language:

| Scheduler | Mechanism |
|---|---|
| SLURM | `#SBATCH --dependency=afterok:<id>` |
| PBS / Torque | `#PBS -W depend=afterok:<id>` |
| SGE / UGE | `#$ -hold_jid <id>` |
| None (background) | the wrapper waits for the previous job's process |

Submit again and the third job queues behind the *second*, not the first, so a
chain of any length lines up in order.

It matters most where there is **no queue** — including this machine — because
nothing else would stop two submissions starting at once and fighting over the
same cores. A job still waiting there shows as **QUEUED** rather than RUNNING,
since its wrapper is alive but the calculation has not begun.

The waiting always happens on the host, never in MoleditPy: close the
application, log out, lose the network, and the chain keeps moving.

#### When the job in front fails

The two mechanisms differ here, and the difference matters:

| Scheduler | A predecessor that fails or is cancelled |
|---|---|
| SLURM, PBS | the jobs behind it **never start** — `afterok` can no longer be satisfied |
| SGE | they run anyway — `-hold_jid` releases on the job ending, not on it succeeding |
| None (background) | they run anyway — `kill -0` stops matching the moment the process is gone |

On SLURM and PBS the queue goes on reporting such a job as PENDING, which reads
as "starting soon" and is the opposite of the truth. Job Manager shows it as
**BLOCKED** instead, names the job that actually died, and writes a line to the
log the moment the predecessor fails. Nothing is cancelled for you — the usual
fix is to correct the input and resubmit — but you are told.

The whole chain is marked, not only the job immediately behind the failure: if
three jobs are queued in a line and the first fails, all the rest are stranded,
and each one gives its slot back rather than holding it for the session.

Tick **...even if that job fails** to ask for `afterany` rather than `afterok`,
which is right when the jobs are independent and only being serialised to share
a machine. The box appears only on SLURM and PBS; the other two release on the
predecessor ending regardless, so there is nothing to choose.

A new job never queues behind a job that is itself blocked: joining a dead
chain would strand it too.

You can still write a dependency by hand in *Extra directives* — anything you
put there lands in the directive block, ahead of the first command, which is
where a scheduler stops reading.

### Running on Windows, with nothing to install

Choose the scheduler **Built-in (Windows, PowerShell)** and the whole workflow runs
through PowerShell: the wrapper, the status checks, the cancel, and the
plugin's own housekeeping. Windows PowerShell 5.1 ships with the operating
system, so nothing needs installing; PowerShell 7 (`pwsh`) is used where both
are present.

The other schedulers all generate bash. On Windows that means Git Bash or WSL —
still supported, and still the right choice if you are submitting to a Linux
cluster from a Windows desktop, since the *cluster* is what runs the script.
The Windows scheduler is for the case where the Windows machine itself is doing
the computing.

Two differences worth knowing:

| | bash | Windows |
|---|---|---|
| A job you cancel | `FAILED (rc=143)` | `LOST` |
| A job killed by Task Manager | `FAILED (rc=137)` | `LOST` |

bash turns a kill signal into an exit code before the wrapper finishes, so the
outcome is recorded. Windows has no such signal: `TerminateProcess` stops the
process dead and nothing runs afterwards, so no exit code is written and the
job reads as `LOST` — which is what "the wrapper never finished" means
everywhere else in this plugin. The remote directory, the log and **Download**
all still work.

The helper queue works here too — a PowerShell runner rather than a bash one,
with the same queue, the same numbered scripts (`job_0001_<id>.ps1`), the same
core accounting, dependencies and pause. Choosing it is the same *Queueing*
setting on the host profile.

One limitation: if a command template ends with a bare PowerShell `exit N` and
no program ran before it, there is nothing left for the wrapper to read and the
job is recorded as failed. Ending a template with a program's own exit code
(the normal case) is read correctly.

## Running several jobs on a machine with no queue

`nohup` is not a scheduler. On a host with no queue, nothing stops five
submissions starting at once and fighting over the same cores and the same
memory. Job Manager offers two ways to fix that, chosen by **Queueing** on the
host profile.

| | Helper queue (default) | Chained lanes |
|---|---|---|
| Where the waiting happens | a small script on the host | the queue's own dependency, or the wrapper |
| Schedules on | cores, memory, and a job count | a job count only |
| A slot frees | the moment a job ends | when the whole lane reaches it |
| Cancel a job that has not started | yes, and its resources come back | it is bound to its predecessor |
| Leaves anything on the host | a directory, and a process while jobs run | nothing at all |
| Needs | a POSIX shell or PowerShell | nothing |

The helper is the default because it is the only one of the two that can
schedule on resources at all: chained lanes fix the order when you submit and
know nothing about cores or memory. A host profile saved before the helper
existed keeps chaining, so an upgrade does not move your jobs onto a different
scheduler unasked.

Both are offered only where there is no scheduler already. On SLURM, PBS or SGE
leave **Run at most** at *no limit*: the queue is doing this, and better.

### What the helper runs at once

Three dials, and they are not the same one:

- **Run at most** caps the *number* of jobs. Left at **no limit** — the default
  — the helper puts no ceiling on the count, and the two budgets below are what
  actually schedule. (Before v0.8.0 "no limit" reached the helper as *one*, so
  an untouched host profile ran strictly one job at a time and nothing on
  screen said why.)
- **Cores available** is the CPU budget. Each job asks for its preset's *CPUs
  per task*, and starts when that many are free.
- **Memory available** is the second budget, and usually the one that matters.
  Each job asks for its preset's *Memory*, and starts when that much is free.

Both are numbers you type. **Ask the host instead** hands the decision to the
helper, which reads the machine's own capacity — off by default, because what a
shared machine reports is all of it, not the share you are entitled to.

So an eight-core workstation with the defaults runs eight single-core jobs
together, or two four-core jobs, and queues the rest. The queue is strict FIFO:
a small job does not jump ahead of a large one that is waiting for room, which
would otherwise starve it. A job asking for more than the machine has is given
the whole machine rather than waiting for ever.

**Cores alone are not enough.** Two jobs asking for 90 GB each must not both
start on a 120 GB machine merely because the cores were free — overcommitting
CPU makes a calculation slow, overcommitting memory gets it killed hours in.
With a memory budget the second waits. A job that asks for no memory waits for
no memory, so nothing is held back by a field you left blank.

**Detect** fills both fields in from the host itself without handing the budget
over, so you can see the numbers and then lower them to leave room for other
users. It counts **physical cores, not hardware threads**: `nproc` reports
twelve on a six-core machine, and a budget of twelve would let two six-core jobs
thrash the same six cores. The helper uses the same count when you tick *Ask the
host instead*, so the dialog and the queue never disagree about the machine.

### The helper's life

Nothing is installed and nothing runs when there is no work. The first
submission creates `~/moleditpy_jobs/.moleditpy_runner/`, uploads the script
and starts it; it dispatches what fits, and **exits as soon as the queue is
empty**. The next submission starts it again. On a shared login node that
matters: there is no daemon of yours sitting there between batches.

The queue is plain numbered shell scripts — `job_0001_<id>.sh` — one per job,
each self-contained. You can read, reorder or empty it over plain `ssh` with
`ls` and `mv`, and a job that has run is still exactly the script that ran it.
Nothing needs this plugin to make sense of it.

**Nothing is cleaned up behind you.** Entries move into `done/` and stay there,
each job keeps its own directory (`<date>_<name>_<id>`, so two jobs of the same
name never land in one), and the helper script itself is named after a digest of
its contents — an upgrade adds a file rather than replacing the one a running
helper is executing. Delete what you no longer want, when you want.

### Chained lanes instead

Set **Queueing** to *Chain the jobs together* and the limit is kept with the
same dependency the scheduler (or the wrapper) already honours: submissions
over the limit are chained behind the **shortest** lane, so seven jobs at a
limit of two become two balanced queues of three and four rather than one long
chain behind a single job.

The limit applies whether or not you asked for chaining: a limit you can switch
off by unticking a box is not a limit, so where one is set the *Run after…*
checkbox steps aside and the wizard tells you which slot you are getting.

Jobs queued this way always use `afterany`: they are independent jobs being
serialised to share a machine, so one failure must not strand the rest of its
lane. Finishing the job at the head of a lane does not open a slot — whatever
was queued behind it takes that lane over. A job that is `BLOCKED` occupies
nothing, since it is never going to run.

### Memory and cores are read from your input

You already state both in the input file, so the wizard reads them when you add
one and fills *Memory* and *CPUs per task* in for you. It never writes over a
value you have already typed.

| Program | Read from |
|---|---|
| ORCA | `%maxcore` × `%pal nprocs` (or `! PALn`) |
| Gaussian | `%mem`, `%nprocshared` |
| Psi4 | `memory 8 GB` |
| NWChem | `memory total 4000 mb` |
| Q-Chem | `MEM_TOTAL` |
| GAMESS | `MWORDS` (× 8 MB) |

**ORCA's `%maxcore` is per core**, which is the trap here: `%maxcore 3000` with
`%pal nprocs 8` is a 24 GB job, not a 3 GB one. Gaussian's `%mem` is a total
and is taken as one. Anything the wizard cannot read leaves the fields alone
rather than guessing.

Untick **Take these two from the input file**, under the same two fields, to
turn the reading off and type both by hand; ticking it again reads the file
already added. The choice is remembered between submissions. The **Command**
line is never written by any of this — it comes from the template dropdown, and
only into a command you have not edited — and the input file itself is never
modified.

### Holding the helper's queue

Where the *Queueing* setting is **Queue them with a helper on the host**, the
host profile grows a **Queue on the host** row:

- **Hold the queue** stops the helper starting anything new. Jobs already
  running are left alone — a pause that killed them would mean throwing away
  however long they have been going. The flag lives on the host, so it outlasts
  the dialog, the session, and the helper's own comings and goings: a helper
  that exits and is started again by the next submission finds the queue still
  held. Untick it to let things move again.
- **Apply limits now** sends all three limits to a helper that is already
  running. Submitting sends them too, so this is for changing your mind while
  jobs are queued — which is exactly when waiting until the next submission is
  no use. The helper re-reads them between jobs, so nothing restarts.
- **Detect** asks the host what it has and fills the two budgets in.

The box shows the queue's real state, read from the host when you select it.
A host set to ask for a password is left alone until you press **Test
Connection**, so that clicking a name in a list never raises a prompt.

### Starting later

Tick **Do not start before** and pick a moment. The job is handed over
immediately — MoleditPy need not be running when it starts — and the scheduler
is told to hold it:

| Scheduler | Mechanism |
|---|---|
| SLURM | `#SBATCH --begin=2026-08-07T22:00:00` |
| PBS / Torque | `#PBS -a 202608072200.00` |
| SGE / UGE | `#$ -a 202608072200.00` |
| None (background) | the wrapper sleeps until that moment |

Useful for a nightly window, or for staying off a shared workstation during the
day. The no-queue wait compares epoch seconds, so a machine in another timezone
still starts at the instant you meant. Chaining and a start time can be combined:
"after that job, and not before 10 pm".

## 4. Watch

The monitor polls every 120 s by default: one status query per host per cycle,
however many jobs you have. The timer stops when nothing is active.

You can go faster — down to 5 s — but under 30 s the toolbar shows **⚠ fast**.
On a shared login node that is the kind of thing admins complain about; against
your own workstation it is fine.

**Refresh Now** forces a cycle immediately (rate limited to once every 10 s).

### Being told when a job ends

The status bar counter and the icon badge both answer "how many are running",
which is a number you have to go and look at. For a calculation that runs for
hours the useful moment is the *transition*, so a desktop notification is
raised when a job finishes, fails, or disappears from the queue — naming the
job and the host.

On by default, unlike the badge: it is transient rather than a lasting change
to how MoleditPy looks. Untick **Notify me when a job ends** in the monitor to
stop it. A desktop with no notification service simply shows nothing; the job
is tracked either way.

### Hosts at work

**Hosts at Work...**, beside *Refresh Now*, opens a live panel: one card per
host with its load average, its memory, and a graph of each over the last
couple of minutes. The load graph is full when the load equals the core count,
which is what a full machine means — not some arbitrary hundred.

It asks each host one small command every **2 seconds** (adjustable, 1–60), and
**only while that window is open**. Closing it stops the timer and hands every
connection back, so a Job Manager left open overnight costs a login node
nothing on its account. The connection is held while the window is up rather
than rebuilt per sample: at that cadence, reconnecting would cost more than the
measurement.

A host that does not answer says so on its own card and the others carry on. A
host set to prompt for a password is skipped entirely — a panel that opens
should not raise a password dialog.

## 5. Results

When a job reaches DONE or FAILED, matching files come back automatically (the
fetch patterns, plus the log always) — **next to the input file** by default,
which is where you are already working. Untick *...next to the input file* on
the wizard and they go to `~/.moleditpy/job_manager/downloads/<timestamp>_<name>/`
instead, which is also where a job with no local input to sit beside puts them.

Each file arrives under a `.moleditpy-part` name and is renamed once the
transfer finishes, so a download cut off half way never leaves a truncated
`.out` sitting in your working directory under its real name.

The most interesting file — `.out`, then `.log`, `.fchk`, `.hess`, `.xyz` — is
handed to the application's file openers, which is how ORCA Result Analyzer and
the Gaussian analyzers pick it up. Untick **Open results automatically** if you
would rather do that yourself.

**Tail Log** reads the last 200 lines of the running job's log without
downloading anything.

## 6. Resubmit

**Resubmit** reopens the wizard with the same host, the same inputs and the
preset *as it was when that job ran* — the snapshot survives editing or deleting
the named preset. Adjust anything and submit again.

**It is a new job, and the old one is left alone.** Resubmitting gets its own
job id, so it gets its own remote directory: the previous run's inputs, log,
outputs and exit code all stay exactly where they were, and its queue entry
stays in `done/`. Nothing is reused and nothing is written over — including
when you resubmit within the same second as the original, which used to land
both runs in one directory.

This holds when the helper has stopped in between, which is the normal state
between batches: the queue directory and the dispatch counter live on the host,
not in the helper, so the next submission starts a fresh helper and takes the
*next* number rather than beginning again at one.

## 7. Keeping, exporting and clearing the list

The row of buttons on the right of the table deals with the list as a whole.

| Button | Does |
|---|---|
| **Save As...** | Saves the list to a `.pmejbs` file — the same records the plugin stores, openable again |
| **Export CSV** | One row per job — state, exit code, timings, remote and local paths, the command |
| **Load Archive...** | Opens a previously cleared list |
| **Clear List...** | Empties the table, after saving it to `archived/` |

**Clearing never deletes.** The current list is written to
`~/.moleditpy/job_manager/archived/jobs_<date>_<time>.pmejbs` first, because a
job's remote directory is often the only way back to results still sitting on
the cluster. If any jobs are still active, the confirmation says so — clearing
stops tracking them but cancels nothing.

Archives are never removed automatically. **To delete one permanently, open the
archived folder** in your file manager; the read-only banner shows its path.

### Opening a job list

Three ways in: **Load Archive...**, **File ▸ Import** in the main window (the
plugin registers `.pmejbs` with the application), or by dropping the file onto
the Job Manager window.

What happens next depends on the file, not on where it sits:

* **Marked archived** (written by Clear List) — shown **read only**. Every
  action is disabled, because an archived job's queue id is stale and its remote
  directory may be long gone. **Back to current jobs** returns you to the live
  table.
* **Not marked archived** (an export, a backup, a colleague's file) — offered as
  the list to work in. Accept and it becomes the file every later change is
  written to, with a banner naming it. This lasts for the session only: a
  restart comes back to your usual list, and **Use the default list** switches
  back immediately.

The flag lives inside the file, so a cleared list stays read-only after being
moved, copied or mailed on.

### The `.pmejbs` format

MoleditPy's extension for a saved job list, alongside `.pmeprj` for a project.
The contents are ordinary JSON — `version`, `archived`, `jobs`, and
`archived_at` on an archived list — so anything that reads JSON can read it. A
`jobs.json` written before the extension existed is still read, and the next
save migrates it.

## Reading job states

| State | Means |
|---|---|
| `SUBMITTED` `PENDING` | queued |
| `RUNNING` `COMPLETING` | on a node |
| `DONE` | wrapper finished, exit code 0 |
| `FAILED` | wrapper finished, non-zero — the code is shown in the table |
| `CANCELLED` | you cancelled it |
| `LOST` | gone from the queue with no exit code recorded |
| `QUEUED` | chained behind another job that has not finished yet |
| `BLOCKED` | somewhere behind a job that failed, under a dependency the queue can never satisfy — it will not start |

`FAILED (rc=143)` is the signature of a job the scheduler killed: 143 is
`128 + SIGTERM`, i.e. walltime exceeded, preemption or a node drain. `130` is
Ctrl-C / SIGINT, `129` is SIGHUP.

`LOST` means the job left the queue without the wrapper writing its exit code —
killed hard (`SIGKILL`, OOM killer), the node fell over, or the job directory
vanished. The remote directory is still listed in the tooltip; **Download** and
**Tail Log** still work, and the log usually says why.

## What it does not do

**No workflow graph.** Chaining is a straight line: each job waits for exactly
one predecessor. There is no fan-out, no "run C after both A and B", and no
retry on failure. For anything branching, write the dependency by hand in
*Extra directives* — `#SBATCH --dependency=afterok:12345,afterok:12346` — which
Job Manager passes through, ahead of the first command, and then tracks
normally.

**The helper queue is not a batch system.** It is FIFO with two resource
budgets and one-predecessor dependencies -- no priorities, no backfill, no
fair share, no reservations, and no accounting. It exists so a workstation
does not thrash, not to replace SLURM.

**No file browser.** Fetch patterns decide what comes back.

**No allocation or accounting queries** — no `sacct`, no `sinfo`, no quota.

### Remote disk is yours to reclaim

Job Manager never deletes anything on a host. Cancelling a job, removing it
from the list, pruning old rows or clearing the whole table all touch the
plugin's own records only — the remote directory, its inputs, its log and its
outputs stay exactly where they are.

That is deliberate: a job directory is often the only way back to results still
on the cluster, and losing it because a row left a table would be the wrong
trade. It does mean the space is never reclaimed for you. `~/moleditpy_jobs/`
is an ordinary directory; delete from it whenever you like, including while the
helper is running — it only ever reads what is under `.moleditpy_runner/`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "SSH authentication failed... batch mode" | The OpenSSH backend cannot answer a password prompt. Use a key or agent, or switch the host to the paramiko backend |
| "Host key verification failed" | Unknown host. Accept the fingerprint when offered, or `ssh` in once by hand |
| Submitted, but no queue id | The submit command printed something unexpected. The job may really be queued — check with `squeue` before resubmitting |
| Everything `LOST` right after submitting | The scheduler on the host profile does not match reality (e.g. SLURM selected on a PBS site) |
| Nothing downloads | The fetch patterns match nothing. Press **Download**: it lists what is actually in the job directory and ticks what matched, so a wrong pattern is visible rather than silent |
| `job.log` is not among the results | Deliberate: it is the wrapper's own log, not the calculation's output, and is never downloaded. **Tail Log** reads it on the host |
| A custom command you saved is not in the dropdown | See below |
| Results in a sub-directory are not fetched | A pattern only reaches as deep as it is written. `*.out` means the job directory; use `scratch/*.out`, `*/*.out`, or `**/*.out` for any depth |
| Poll errors, then silence | Per-host backoff, up to 15 minutes. **Refresh Now** clears it |
| Jobs sit at `PENDING` on the helper and nothing starts | The queue is held (**Hold the queue** in Hosts…), or the job in front needs more cores or memory than are free. The helper is strict FIFO, so a small job behind a large one waits with it |
| Only one job runs at a time on the helper | **Run at most** is set to 1, or the budgets are smaller than two jobs need. **Detect** shows what the machine actually has |
| A job asks for more memory than the machine has | It is given the whole machine and runs alone, rather than waiting for ever |
| The helper is not running between batches | By design: it exits as soon as its queue is empty and the next submission starts it again |
| The wizard filled in a memory figure you did not expect | It was read from the input. ORCA's `%maxcore` is **per core**, so it is multiplied by `%pal nprocs`. Overwrite the field and it is left alone |
| `command not found` for something that works when you ssh in | See below — it is almost always the guard at the top of `~/.bashrc` |

### A saved command template does not appear

The **Template...** dropdown is rebuilt from three sources, in this order: the
built-in list, then a separator, then your own saved templates, then the two
actions at the bottom. A template you saved and cannot see is almost always one
of these:

- **You saved it under a name that already existed.** Saving replaces a
  template of the same name rather than adding a second one with it, so the
  list gets no longer.
- **The command field was empty when you pressed "Save current command as...".**
  Nothing is saved without a command, and the dialog says so rather than
  writing an empty entry.
- **You are looking at a different MoleditPy installation.** Templates live in
  `~/.moleditpy/job_manager/settings.json` under `command_templates`, outside
  the plugin folder, so they survive plugin updates — but they are per user
  account, not per project.
- **The list is long and yours is below the fold.** Saved templates come after
  every built-in one; the dropdown scrolls.

To check what is actually stored:

```bash
python -c "import json,os;print(json.load(open(os.path.expanduser('~/.moleditpy/job_manager/settings.json')))['prefs'].get('command_templates'))"
```

Saving a template keeps the **fetch patterns** with the command, so a template
saved for a program brings back that program's files. A template saved before
this was true has no patterns of its own and leaves the field alone.

### The wizard did not fill in the fetch patterns

Choosing a program from **Template...** fills them in for that program — ORCA
writes `.gbw` and `.hess`, Gaussian `.chk` and `.fchk`, VASP files with no
extension at all. It leaves the field alone once you have edited it, because a
field you have changed is a decision.

For an unambiguous extension the template is applied automatically when the
file is added. `.inp` is not one of them: ORCA, CP2K and GAMESS all use it, so
the wizard offers the three rather than guessing.

### "command not found", but it works when I ssh in

The job runs `moleditpy_run.sh` through a shell that is **not interactive**, and
that one difference is behind nearly every one of these.

**1. Your `~/.bashrc` stops before your line.** The stock Debian and Ubuntu file
opens with:

```bash
# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac
```

Sourcing that file from a job returns at line 6. Everything below — including
the `export PATH=...` you added at the bottom — never runs. Ticking
**Environment** does not help, because the file itself declines to do anything.
Logging in by hand works because *that* shell is interactive.

Confirm it, and see exactly what the job sees:

```bash
ssh myhost 'shopt -s expand_aliases
  [ -f /etc/profile ] && . /etc/profile
  [ -f ~/.bash_profile ] && . ~/.bash_profile
  [ -f ~/.profile ] && . ~/.profile
  [ -f ~/.bashrc ] && . ~/.bashrc
  type mycommand; echo "PATH=$PATH"'

ssh myhost 'grep -n "case \$- in" ~/.bashrc; grep -n mycommand ~/.bashrc'
```

A definition *below* the guard's line number is the whole story. Three fixes, in
order of reliability:

1. **Put the export in the host's Login commands** (Hosts… → Advanced). It runs
   unconditionally, in the job script and around every command sent to the host,
   and depends on no dotfile at all.
2. **Move the export above the guard** in `~/.bashrc` — fixes it for every tool
   you drive over SSH, not just this one.
3. **Move it to `~/.bash_profile` or `~/.profile`**, which carry no such guard
   and which **Environment** also reads.

**2. It is an alias.** Bash expands aliases only in an interactive shell, so
`alias myorca=...` is read as a plain word. The preamble now sets
`shopt -s expand_aliases` before sourcing, which makes aliases work — provided
they are reached at all, i.e. provided (1) is not also in the way. A shell
*function* has never needed this.

**3. It is on `PATH` only for a login shell.** Site software often lives behind
`module`, whose own initialisation is in `/etc/profile.d`. **Environment** reads
`/etc/profile`, which reads those — so tick it, then `module load ...` in
**Login commands**.

**4. The name resolves to the wrong program.** `type -a orca` on a Linux desktop
usually reports `/usr/bin/orca` — the GNOME **screen reader**. A job calling it
prints "The following are not valid: <file>" and "Cannot start the screen reader
because it cannot connect to the Desktop", then exits 1 with no output file. Use
the absolute path, which ORCA requires anyway for `%pal nprocs > 1`.
