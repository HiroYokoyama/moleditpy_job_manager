"""Native Windows: no queue, no POSIX shell, PowerShell all the way down.

Every other backend here generates bash. That is fine on a cluster and fine on
a Mac, and on Windows it means "install Git Bash first" -- which is a real
answer, but not one that works on a machine somebody else administers.

This scheduler is the same contract expressed in PowerShell, and the pieces
that had to be rethought rather than translated are these:

**The sentinel.** bash writes the exit code from a ``trap ... EXIT``, which
runs however the script leaves. PowerShell's equivalent is ``try/finally``, and
it holds for the same reasons: a payload that calls ``exit`` itself, or a
pre-command that throws, still reaches the ``finally``.

**Signals do not exist.** bash turns SIGTERM into exit 143 so a job the
scheduler killed is recorded as FAILED rather than as a clean success. Windows
has no such signal: ``TerminateProcess`` stops the process dead and no
``finally`` runs. A killed job therefore leaves no sentinel and is classified
LOST, which is exactly what "the wrapper never finished" means everywhere else
-- so the difference is visible to the user rather than silently wrong.

**$LASTEXITCODE is not $?.** It is set by native executables only, and is left
over from the *previous* command when the current one is a cmdlet. It is read
immediately and defaulted, or a job whose command is a cmdlet would inherit
some earlier program's status.

**ASCII, not UTF-8.** ``Set-Content`` in Windows PowerShell 5.1 writes a BOM
with most encodings, and a BOM in front of an exit code makes ``int()`` fail on
the reading side. This exact mistake once broke every CI leg in another repo in
this workspace.
"""

from __future__ import annotations

import time
from typing import Dict, Iterable, List, Sequence

from ..models import SubmitPreset, sanitize_name
from .base import (
    CORES_TAG,
    MEMORY_TAG,
    STATE_RUNNING,
    Scheduler,
    format_command,
    register,
    requested_cores,
    requested_memory_mb,
)

#: The generated wrapper. ``.ps1`` so PowerShell will run it at all.
SCRIPT_NAME = "moleditpy_run.ps1"

#: How PowerShell is invoked: no profile (a user's profile can print banners
#: that corrupt captured output, and costs a second on every launch), and
#: Bypass because a generated script is unsigned and the default policy on a
#: workstation refuses it.
POWERSHELL_ARGS = "-NoProfile -ExecutionPolicy Bypass -File"

#: Seconds between checks while a wrapper waits for its start time.
WAIT_POLL_SECONDS = 5


def ps_quote(value: str) -> str:
    """Quote for a PowerShell single-quoted string.

    Single quotes, not double: inside double quotes PowerShell expands ``$``
    and backticks, and a remote path is not something to hand to an expression
    parser. The only escape needed is a doubled single quote.
    """
    return "'" + str(value or "").replace("'", "''") + "'"


class WindowsScheduler(Scheduler):
    """Run jobs directly on a Windows machine, tracked by process id."""

    name = "windows"
    label = "Built-in (Windows, PowerShell)"
    order = 20
    queue_directives = False
    script_name = SCRIPT_NAME
    supports_chaining = True
    # Liveness is a process check, so a predecessor that failed is simply gone
    # and whatever waited for it starts as normal.
    chain_releases_on_failure = True

    def directives(self, job_name: str, preset: SubmitPreset, log_file: str) -> List[str]:
        # No queue reads these, so the head of the script is where the request
        # is recorded -- the same tags the helper queue speaks, so a wrapper
        # found on the machine says what it was asked for.
        lines = [f"# job: {job_name}", f"{CORES_TAG} {requested_cores(preset)}"]
        memory = requested_memory_mb(preset)
        if memory:
            lines.append(f"{MEMORY_TAG} {memory}")
        return lines

    def build_script(
        self,
        job_name: str,
        preset: SubmitPreset,
        input_name: str,
        log_file: str,
        run_after: str = "",
        start_after: float = 0.0,
        remote_dir: str = "",
        run_after_any: bool = False,
        sentinel: str = "",
        preamble: Sequence[str] = (),
        relay_lines: Sequence[str] = (),
    ) -> str:
        """The whole wrapper, in PowerShell rather than bash."""
        from ..models import SENTINEL_NAME

        # Per job wherever the directory is shared; see Scheduler.build_script.
        sentinel = sentinel or SENTINEL_NAME
        # And sanitised here for the same reason it is there: {name} reaches a
        # command line, and the preview must show the name the job will have.
        job_name = sanitize_name(job_name)

        # The directive block is built by directives() and used here rather
        # than being written out a second time: this scheduler overrides
        # build_script wholesale, so a header added to directives() alone would
        # never reach the script.
        lines = list(self.directives(job_name, preset, log_file)) + [
            "$ErrorActionPreference = 'Continue'",
            # `>` in Windows PowerShell 5.1 is Out-File, whose default encoding
            # is UTF-16 with a BOM. A command template of the usual shape --
            # `orca in.inp > out.out` -- would therefore write an output file
            # that no quantum-chemistry parser can read, this plugin's own
            # analyzers included. This makes every redirect in the template
            # write plain text instead. Use `cmd /c "prog > out"` for a
            # byte-exact copy of a program that emits non-ASCII.
            "$PSDefaultParameterValues['Out-File:Encoding'] = 'ascii'",
        ]
        if remote_dir:
            # Baked in, never derived from $PSScriptRoot: the script may be
            # copied elsewhere, and a wrapper that guesses its own directory is
            # how a sentinel once ended up somewhere nobody read it.
            lines.append(f"Set-Location -LiteralPath {ps_quote(remote_dir)}")
        lines.append("if (-not $?) { exit 1 }")
        lines.append(f"Remove-Item -Force -ErrorAction SilentlyContinue {ps_quote(sentinel)}")
        lines += self._start_time_block(start_after)
        lines += self._predecessor_wait_block(run_after)
        lines += [
            "$__moleditpy_rc = 1",
            # Distinguishes "the payload returned" from "the payload called
            # exit and took the script with it". Without it, a command template
            # ending in `exit` never reaches the assignment below and the
            # sentinel keeps its placeholder 1 -- so a job that exited 0 was
            # recorded as FAILED. The bash wrapper had the same bug in another
            # shape, which is why this is tested by running it.
            "$__moleditpy_done = $false",
            "try {",
        ]
        # Inside the try, not before it: a relay copy that throws is then
        # caught by the same catch block a failing payload is, and recorded
        # through the same finally -- rather than skipping both entirely.
        for line in relay_lines or ():
            lines.append(f"    {line}")
        for command in preamble or []:
            if command.strip():
                lines.append(f"    {command.strip()}")
        for module in preset.modules or []:
            if module.strip():
                lines.append(f"    # module: {module.strip()}")
        for command in preset.pre_commands or []:
            if command.strip():
                lines.append(f"    {command.strip()}")
        lines += [
            f"    {format_command(preset.command_template, input_name, preset, job_name, remote_dir)}",
            # Read at once and defaulted: $LASTEXITCODE is set by native
            # executables only, and holds the *previous* program's status when
            # the command was a cmdlet.
            "    $__moleditpy_rc = $LASTEXITCODE",
            "    if ($null -eq $__moleditpy_rc) { $__moleditpy_rc = 0 }",
            "    $__moleditpy_done = $true",
            "} catch {",
            "    $__moleditpy_rc = 1",
            "} finally {",
            # Left early: the payload called exit. Its own program's status is
            # still the best answer available. A bare PowerShell `exit N` with
            # no program before it leaves nothing to read, and stays 1 -- the
            # safe direction, since a failure reported as success is the one
            # mistake that must not happen.
            "    if (-not $__moleditpy_done -and $null -ne $LASTEXITCODE) "
            "{ $__moleditpy_rc = $LASTEXITCODE }",
            # ASCII: Set-Content writes a BOM with most encodings in Windows
            # PowerShell 5.1, and a BOM in front of the exit code makes it
            # unparseable on the reading side.
            # Beside itself, then renamed: Set-Content truncates before it
            # writes, and a poll landing in that window reads an empty file --
            # indistinguishable from a missing one, so a finished job would be
            # reported LOST. Move-Item -Force replaces in one step.
            f"    Set-Content -Path {ps_quote(sentinel + '.tmp')} "
            "-Value $__moleditpy_rc -Encoding ascii",
            f"    Move-Item -LiteralPath {ps_quote(sentinel + '.tmp')} "
            f"-Destination {ps_quote(sentinel)} -Force",
            "}",
            "exit $__moleditpy_rc",
            "",
        ]
        return "\r\n".join(lines)

    def _start_time_block(self, start_after: float) -> List[str]:
        target = int(start_after or 0)
        if target <= 0:
            return []
        return [
            f"# Scheduled: hold until epoch {target}"
            f" ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(target))} here).",
            f"$__target = [DateTimeOffset]::FromUnixTimeSeconds({target}).LocalDateTime",
            f"while ((Get-Date) -lt $__target) {{ Start-Sleep -Seconds {WAIT_POLL_SECONDS} }}",
        ]

    def _predecessor_wait_block(self, run_after: str) -> List[str]:
        """Wait for another process on this machine to finish.

        On the host, not in MoleditPy: a chain held on this side would stall
        the moment the application was closed.
        """
        pid = str(run_after or "").strip()
        if not pid.isdigit():
            return []
        return [
            f"# Chained: wait for process {pid} to finish first.",
            f"while (Get-Process -Id {pid} -ErrorAction SilentlyContinue) "
            f"{{ Start-Sleep -Seconds {WAIT_POLL_SECONDS} }}",
        ]

    def dependency_directives(self, after_id: str, any_outcome: bool = False) -> List[str]:
        return []

    def submit_command(self, script_name: str, log_file: str) -> str:
        """Start the wrapper detached and print its process id.

        ``-PassThru`` gives the process object, whose ``Id`` is what the poller
        tracks. stdout and stderr must go to *different* files: PowerShell
        refuses to redirect both to one.
        """
        err_file = (log_file or "job.log") + ".err"
        # Every path is made absolute against the current location first.
        # Start-Process resolves a relative path against PowerShell's location
        # in pwsh 7 but against the process's working directory in Windows
        # PowerShell 5.1, and the two are not the same place here -- the caller
        # got to this directory with Set-Location.
        return (
            "$d = (Get-Location).Path; "
            "$p = Start-Process -FilePath powershell "
            "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"
            f"(Join-Path $d {ps_quote(script_name)}) "
            f"-RedirectStandardOutput (Join-Path $d {ps_quote(log_file)}) "
            f"-RedirectStandardError (Join-Path $d {ps_quote(err_file)}) "
            "-WorkingDirectory $d -WindowStyle Hidden -PassThru; $p.Id"
        )

    def parse_submit_output(self, stdout: str, stderr: str) -> str:
        for line in reversed((stdout or "").splitlines()):
            token = line.strip()
            if token.isdigit():
                return token
        return ""

    def status_command(self, username: str, job_ids: Iterable[str]) -> str:
        pids = [str(j).strip() for j in job_ids if str(j).strip().isdigit()]
        if not pids:
            return "exit 0"
        joined = ",".join(pids)
        return (
            f"Get-Process -Id {joined} -ErrorAction SilentlyContinue | ForEach-Object {{ $_.Id }}"
        )

    def parse_status(self, stdout: str) -> Dict[str, str]:
        states: Dict[str, str] = {}
        for line in (stdout or "").splitlines():
            token = line.strip()
            if token.isdigit():
                states[token] = STATE_RUNNING
        return states

    def cancel_command(self, job_id: str) -> str:
        """Kill the wrapper *and* its children.

        ``Stop-Process`` alone leaves the payload running: the wrapper is a
        PowerShell host that launched the real program as a child, and killing
        the parent orphans it. ``taskkill /T`` takes the tree.
        """
        pid = str(job_id or "").strip()
        if not pid.isdigit():
            # Never interpolated unchecked: a job list can come from anywhere,
            # and this string is run on the user's machine.
            return "exit 0"
        return f"taskkill /PID {pid} /T /F"


WINDOWS = register(WindowsScheduler())
