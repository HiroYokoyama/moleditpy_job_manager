# Real scheduler validation

`Real Scheduler Validation` provisions an actual SLURM, OpenPBS or Grid Engine
daemon in a disposable GitHub-hosted Ubuntu VM and exercises the plugin through
an actual, loopback-only OpenSSH connection. It uses only synthetic inputs and a
new `schedulerlab` account. It never opens a developer's JobStore or host settings.

Run it manually after this workflow exists on the default branch:

```bash
python scripts/dispatch_scheduler_validation.py --ref main --scheduler all --wait
python scripts/dispatch_scheduler_validation.py --ref main --scheduler pbs --wait
```

The helper requires Python and an authenticated `gh` CLI. It stores no token and
uses a unique dispatch ID when waiting, so it cannot accidentally report another
run's result. `--dry-run` prints the dispatch arguments without contacting GitHub.

The ordinary test matrix is unchanged. This separate workflow runs only by
explicit manual dispatch, never automatically on push, PR or release. SLURM/Grid Engine use
Ubuntu packages; OpenPBS v23.06.06 is built from its pinned upstream commit.
Grid Engine's parallel test explicitly configures an `smp` PE; production sites
must supply their own PE name in extra directives.

Cases cover upload/download, filenames with spaces in the parent directory,
exit-code evidence, requested CPU allocation, cancellation, queued dependencies,
failure/after-any dependencies and scheduled start across different client/host
time zones. A passing run validates the installed implementation/version; it does
not certify every Torque or commercial UGE installation, multinode MPI, GPU
resource configuration or site-specific queue policy.

Artifacts contain only setup/exercise/daemon logs and a JSON report. SSH private
keys and scheduler authentication/database files are never uploaded. Provisioning
scripts are for disposable CI VMs, not for an existing HPC login node.
