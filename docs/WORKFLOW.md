# Standard workflow

The normal path, end to end, plus what to do when a job goes wrong.

## 1. Add a host, once

**Job Manager → Job Monitor → Hosts...**

| Field | Notes |
|---|---|
| Hostname / Username / Port | Or just the alias from your `~/.ssh/config` |
| Backend | **OpenSSH** unless the host needs a password, or **This machine** for no SSH at all |
| Scheduler | SLURM, PBS/Torque, SGE/UGE, or None (background process) |
| Private key | Optional. Leave empty to use your agent and `ssh_config` |
| Jump host | `user@bastion` — OpenSSH backend only |
| Remote root | Where job directories are created, default `~/moleditpy_jobs` |
| Login commands | Run before every remote command, e.g. `module purge` |

Press **Test Connection**. It round-trips `echo` + `hostname` and reports the
remote name. An unknown host key stops here and offers to show you the
fingerprint — see [SECURITY_MODEL.md](SECURITY_MODEL.md).

## 2. Write an input

Any MoleditPy input generator. ORCA Input Generator Pro and Gaussian Input
Generator Pro have a **Submit to Cluster...** button next to Save that hands the
file straight to the wizard, prefilled — no file picker, no retyping the name.

Everything else: save the file, then **New Job...** and add it by hand.

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

### Placeholders

Both spellings work, so you never have to fight the shell:

| Tag | Expands to |
|---|---|
| `{input}` / `[input]` | the first input file's name |
| `{basename}` | the same thing, spelled differently |
| `{stem}` / `[stem]` | that name without its extension |
| `{output}` / `[output]` | `<stem>.out` |
| `{ntasks}` `{nodes}` `{cpus}` | the resource fields |
| `{cpus_per_task}` | a longer spelling of `{cpus}` |
| `{memory}` `{walltime}` `{queue}` | likewise |

Unknown tags are left verbatim, and shell syntax that merely looks like a tag —
`awk '{print $1}'`, `if [ -f x ]` — is passed through untouched.

### Saving your own template

Type a command, then **Template... → Save current command as...** and give it a
name. It is stored in `settings.json` next to your hosts and presets, appears in
the same dropdown from then on, and can be removed with **Delete a saved
template...**.

### Running on this machine

Pick **This machine (no SSH)** as the backend and the same workflow runs
locally: no hostname, no keys, no network. *Remote root* becomes an ordinary
directory here, and "upload" and "download" are file copies.

It still needs a POSIX shell, because the run script is bash — free on macOS and
Linux, and on Windows it means Git Bash or WSL. The Hosts dialog says so if it
cannot find one.

### Running jobs one after another

On a host with **no queue** — that includes this machine — two submissions would
otherwise start at once and fight over the same cores. Tick **Run after the job
already queued on this host** in the wizard and the job waits for the previous
one to finish first. The wizard names the job it will follow.

Submit again and the third job queues behind the *second*, not the first, so a
chain of any length lines up in order. A job still waiting shows as **QUEUED**
in the table rather than RUNNING, since its wrapper is alive but the calculation
has not started.

The waiting happens on the machine itself, not in MoleditPy: close the
application, log out, lose the network, and the chain keeps moving. A
predecessor that is killed or already gone does not hold anything up — the next
job simply starts.

Real schedulers do not offer this, because SLURM, PBS and SGE already decide the
order themselves. For a dependency there, put your scheduler's own flag in
*Extra directives*, e.g. `#SBATCH --dependency=afterok:12345`.

## 4. Watch

The monitor polls every 120 s by default: one status query per host per cycle,
however many jobs you have. The timer stops when nothing is active.

You can go faster — down to 5 s — but under 30 s the toolbar shows **⚠ fast**.
On a shared login node that is the kind of thing admins complain about; against
your own workstation it is fine.

**Refresh Now** forces a cycle immediately (rate limited to once every 10 s).

## 5. Results

When a job reaches DONE or FAILED, matching files come back automatically (the
fetch patterns, plus the log always). They land in
`~/.moleditpy/job_manager/downloads/<timestamp>_<name>/` unless you set another
download root.

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

`FAILED (rc=143)` is the signature of a job the scheduler killed: 143 is
`128 + SIGTERM`, i.e. walltime exceeded, preemption or a node drain. `130` is
Ctrl-C / SIGINT, `129` is SIGHUP.

`LOST` means the job left the queue without the wrapper writing its exit code —
killed hard (`SIGKILL`, OOM killer), the node fell over, or the job directory
vanished. The remote directory is still listed in the tooltip; **Download** and
**Tail Log** still work, and the log usually says why.

## What it does not do

**No dependencies on a real scheduler.** Chaining is offered only where nothing
else serialises the work (the no-queue mode, including this machine). On SLURM,
PBS or SGE the queue decides the order, and a dependency belongs in *Extra
directives* — `#SBATCH --dependency=afterok:12345` — which Job Manager passes
through and then tracks normally.

**No file browser.** Fetch patterns decide what comes back.

**No allocation or accounting queries** — no `sacct`, no `sinfo`, no quota.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "SSH authentication failed... batch mode" | The OpenSSH backend cannot answer a password prompt. Use a key or agent, or switch the host to the paramiko backend |
| "Host key verification failed" | Unknown host. Accept the fingerprint when offered, or `ssh` in once by hand |
| Submitted, but no queue id | The submit command printed something unexpected. The job may really be queued — check with `squeue` before resubmitting |
| Everything `LOST` right after submitting | The scheduler on the host profile does not match reality (e.g. SLURM selected on a PBS site) |
| Nothing downloads | The fetch patterns match nothing. `*.out` is not `*.log` |
| Poll errors, then silence | Per-host backoff, up to 15 minutes. **Refresh Now** clears it |
