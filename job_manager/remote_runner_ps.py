"""The remote runner again, for a host with no POSIX shell.

Same queue, same rules, same guarantees as :mod:`remote_runner` -- numbered
self-contained scripts, claim-by-move, exit as soon as the queue empties, and
the re-check after releasing the lock that makes exiting safe. Only the
language differs, and the constants and the naming are shared rather than
copied, so the two cannot drift apart on what an entry is called.

What PowerShell forces to be different:

**``$pid`` is taken.** It is an automatic variable holding *this* process's id,
so a job's pid is never stored in it -- a runner that overwrote ``$pid`` would
reap whichever process happened to match.

**``&&`` does not exist** in Windows PowerShell 5.1: it is a parser error, not
a no-op, so every sequence is ``;`` and every conditional is spelled out.

**Move-Item fails when the destination exists**, which is what makes claiming a
job safe here: two runners cannot both move the same entry out of ``queue``.

**Liveness is ``Get-Process``, not ``kill -0``.** A pid that has been reused by
the operating system would look alive; the lock's pid is written by the process
that started the runner, so the window for that is a few milliseconds at
startup rather than the life of the queue.
"""

from __future__ import annotations

from typing import List

from .remote_runner import (
    AFTER_TAG,
    CORES_NAME,
    CORES_TAG,
    PAUSED_NAME,
    REQUIRE_SUCCESS_TAG,
    RUNNER_LOG_NAME,
    RUNNER_POLL_SECONDS,
    SLOTS_NAME,
    STATUS_BLOCKED,
    SUBDIRS,
)
from .schedulers.windows import ps_quote

#: The generated runner. ``.ps1`` so PowerShell will run it at all.
RUNNER_SCRIPT_NAME = "moleditpy_runner.ps1"
#: Queue entries are PowerShell scripts here.
ENTRY_SUFFIX = ".ps1"

#: How the runner starts a job, and how the plugin starts the runner.
_PS_ARGS = "'-NoProfile','-ExecutionPolicy','Bypass','-File'"


def _join(*parts: str) -> str:
    """Join a Windows path, tolerating either separator on the way in."""
    cleaned = [str(part).replace("/", "\\").strip("\\") for part in parts if part]
    head = str(parts[0]).replace("/", "\\").rstrip("\\")
    return "\\".join([head] + cleaned[1:]) if len(cleaned) > 1 else head


def build_job_script(
    job_dir: str,
    script_name: str,
    log_name: str,
    entry: str,
    directory: str,
    job_name: str = "",
    after_job_id: str = "",
    require_success: bool = True,
    cores: int = 1,
) -> str:
    """The small script the queue holds for one job.

    The header is comments, exactly as in the bash flavour, so the runner reads
    the same tags and the script still runs by hand. The exit code is written
    to ``status/<entry>`` because the runner has no other way to learn it: the
    wrapper's own sentinel lives in the job directory.
    """
    status_path = _join(directory, "status", entry)
    lines = []
    if job_name:
        lines.append(f"# MoleditPy job: {job_name}")
    lines.append(f"{CORES_TAG} {max(1, int(cores or 1))}")
    if after_job_id:
        lines.append(f"{AFTER_TAG} {after_job_id}")
        lines.append(f"{REQUIRE_SUCCESS_TAG} {1 if require_success else 0}")
    lines += [
        f"Set-Location -LiteralPath {ps_quote(job_dir)}",
        # Start-Process rather than `&` with a `>` redirect, for two reasons.
        # Every path is absolute because this script is started by the runner,
        # so the working directory it inherits is the *runner's*. And `>` in
        # Windows PowerShell 5.1 is Out-File, which writes UTF-16 with a BOM --
        # the job's log would come back in an encoding nothing downstream can
        # read. Start-Process copies the child's bytes through untouched.
        "$__moleditpy_p = Start-Process -FilePath powershell -ArgumentList "
        f"{_PS_ARGS},{ps_quote(_join(job_dir, script_name))} "
        f"-WorkingDirectory {ps_quote(job_dir)} "
        f"-RedirectStandardOutput {ps_quote(_join(job_dir, log_name))} "
        f"-RedirectStandardError {ps_quote(_join(job_dir, log_name + '.err'))} "
        "-WindowStyle Hidden -Wait -PassThru",
        "$__moleditpy_rc = $__moleditpy_p.ExitCode",
        "if ($null -eq $__moleditpy_rc) { $__moleditpy_rc = 0 }",
        f"Set-Content -Path {ps_quote(status_path)} -Value $__moleditpy_rc -Encoding ascii",
        "exit $__moleditpy_rc",
        "",
    ]
    return "\r\n".join(lines)


def build_runner_script(directory: str, poll_seconds: int = RUNNER_POLL_SECONDS) -> str:
    """The runner itself, with its own directory baked in."""
    quoted = ps_quote(directory)
    poll = max(1, int(poll_seconds))
    return "\r\n".join(
        [
            "# MoleditPy remote job runner. Runs the scripts in queue\\ in name order,",
            "# at most `slots` at a time, and exits as soon as nothing is left to run.",
            f"Set-Location -LiteralPath {quoted}",
            # Set-Location moves PowerShell's *location*; whether Start-Process
            # resolves a relative path against that or against the process's
            # working directory differs between Windows PowerShell 5.1 and
            # pwsh 7. Everything handed to Start-Process is therefore absolute,
            # so neither reading is wrong.
            f"$__moleditpy_dir = {quoted}",
            "",
            "function Get-DirCount($name) {",
            "    return @(Get-ChildItem -LiteralPath $name -File "
            "-ErrorAction SilentlyContinue).Count",
            "}",
            "",
            "function Read-Limit($file, $fallback) {",
            "    # Re-read every pass, so the limits can change without a restart.",
            "    $v = Get-Content -LiteralPath $file -ErrorAction SilentlyContinue | "
            "Select-Object -First 1",
            "    if ($v -match '^\\d+$' -and [int]$v -gt 0) { return [int]$v }",
            "    return $fallback",
            "}",
            "",
            f"function Get-Slots {{ return (Read-Limit {ps_quote(SLOTS_NAME)} 1) }}",
            "",
            "function Get-TotalCores {",
            "    $n = [Environment]::ProcessorCount",
            "    if ($n -lt 1) { $n = 1 }",
            f"    return (Read-Limit {ps_quote(CORES_NAME)} $n)",
            "}",
            "",
            "function Get-Header($path, $tag) {",
            "    foreach ($line in (Get-Content -LiteralPath $path "
            "-ErrorAction SilentlyContinue)) {",
            "        if ($line.StartsWith($tag)) { return $line.Substring($tag.Length).Trim() }",
            "    }",
            "    return ''",
            "}",
            "",
            "function Get-JobCores($path) {",
            f"    $v = Get-Header $path {ps_quote(CORES_TAG)}",
            "    if ($v -match '^\\d+$' -and [int]$v -gt 0) { return [int]$v }",
            "    return 1",
            "}",
            "",
            "function Get-UsedCores {",
            "    $total = 0",
            "    foreach ($f in @(Get-ChildItem -LiteralPath 'running' -File "
            "-ErrorAction SilentlyContinue)) {",
            "        $total += (Get-JobCores $f.FullName)",
            "    }",
            "    return $total",
            "}",
            "",
            "# Where a job id currently is, as @(dir, entry). $null if never queued here.",
            "function Find-Entry($jobId) {",
            "    foreach ($d in @('queue','running','done')) {",
            "        $m = @(Get-ChildItem -LiteralPath $d -File -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Name -like ('*_' + $jobId + '.*') })",
            "        if ($m.Count -gt 0) { return @($d, $m[0].Name) }",
            "    }",
            "    return $null",
            "}",
            "",
            "function Block-Entry($entry) {",
            "    Move-Item -LiteralPath ('queue\\' + $entry) -Destination ('done\\' + $entry) "
            "-ErrorAction SilentlyContinue",
            f"    Set-Content -Path ('status\\' + $entry) -Value {ps_quote(STATUS_BLOCKED)} "
            "-Encoding ascii",
            "}",
            "",
            "function Invoke-Reap {",
            "    foreach ($f in @(Get-ChildItem -LiteralPath 'running' -File "
            "-ErrorAction SilentlyContinue)) {",
            "        $entry = $f.Name",
            "        # Not $pid: that is an automatic variable holding this",
            "        # process's own id, and writing to it would make the check below",
            "        # test the runner instead of the job.",
            "        $jobPid = Get-Content -LiteralPath ('pids\\' + $entry) "
            "-ErrorAction SilentlyContinue | Select-Object -First 1",
            "        if ($jobPid -match '^\\d+$' -and "
            "(Get-Process -Id ([int]$jobPid) -ErrorAction SilentlyContinue)) { continue }",
            "        # An empty pid file means the job never started; either way it is",
            "        # over, and leaving it here would hold its cores for ever.",
            "        Move-Item -LiteralPath ('running\\' + $entry) "
            "-Destination ('done\\' + $entry) -ErrorAction SilentlyContinue",
            "        Remove-Item -LiteralPath ('pids\\' + $entry) -Force "
            "-ErrorAction SilentlyContinue",
            "    }",
            "}",
            "",
            "# True when everything $entry waits for has happened. A dependency that can",
            "# never be satisfied is moved aside rather than left in the queue: it would",
            "# otherwise keep this runner alive for ever, and a runner that exits when",
            "# the queue empties must have no way to be stuck with an immortal queue.",
            "function Test-Ready($entry) {",
            f"    $after = Get-Header ('queue\\' + $entry) {ps_quote(AFTER_TAG)}",
            "    if (-not $after) { return $true }",
            "    $loc = Find-Entry $after",
            "    if ($null -eq $loc) { Block-Entry $entry; return $false }",
            "    if ($loc[0] -ne 'done') { return $false }",
            f"    $need = Get-Header ('queue\\' + $entry) {ps_quote(REQUIRE_SUCCESS_TAG)}",
            "    if ($need -ne '1') { return $true }",
            "    $rc = Get-Content -LiteralPath ('status\\' + $loc[1]) "
            "-ErrorAction SilentlyContinue | Select-Object -First 1",
            "    if ($rc -eq '0') { return $true }",
            "    Block-Entry $entry",
            "    return $false",
            "}",
            "",
            "function Invoke-Dispatch {",
            "    # Pausing stops new work only. Killing what is already running would",
            "    # make 'pause' mean 'throw away the last six hours'.",
            f"    if (Test-Path -LiteralPath {ps_quote(PAUSED_NAME)}) {{ return }}",
            "    $cap = Get-TotalCores",
            "    foreach ($f in @(Get-ChildItem -LiteralPath 'queue' -File "
            "-ErrorAction SilentlyContinue | Sort-Object Name)) {",
            "        if ((Get-DirCount 'running') -ge (Get-Slots)) { break }",
            "        $entry = $f.Name",
            "        if (-not (Test-Ready $entry)) { continue }",
            "        $want = Get-JobCores ('queue\\' + $entry)",
            "        # A job asking for more than the machine has would otherwise wait",
            "        # for ever; give it everything, so it runs on its own.",
            "        if ($want -gt $cap) { $want = $cap }",
            "        if (((Get-UsedCores) + $want) -gt $cap) {",
            "            # Strict FIFO: wait for room rather than letting small jobs",
            "            # jump the queue, which would starve anything large.",
            "            break",
            "        }",
            "        # Claiming a job *is* moving it: Move-Item fails when the",
            "        # destination exists, so two runners cannot both win.",
            "        try {",
            "            Move-Item -LiteralPath ('queue\\' + $entry) "
            "-Destination ('running\\' + $entry) -ErrorAction Stop",
            "        } catch { continue }",
            "        $proc = Start-Process -FilePath powershell -ArgumentList "
            f"{_PS_ARGS},(Join-Path $__moleditpy_dir ('running\\' + $entry)) "
            "-WorkingDirectory $__moleditpy_dir -WindowStyle Hidden -PassThru",
            "        Set-Content -Path ('pids\\' + $entry) -Value $proc.Id -Encoding ascii",
            "    }",
            "}",
            "",
            "while ($true) {",
            "    Invoke-Reap",
            "    Invoke-Dispatch",
            "    if ((Get-DirCount 'running') -eq 0 -and (Get-DirCount 'queue') -eq 0) {",
            "        Remove-Item -LiteralPath 'lock' -Recurse -Force -ErrorAction SilentlyContinue",
            "        # Look again now the lock is gone. A job enqueued between the test",
            "        # above and the release would otherwise sit in the queue for ever:",
            "        # whoever put it there saw a live runner, and so did not start one.",
            "        if ((Get-DirCount 'queue') -eq 0) { exit 0 }",
            "        # Something arrived. Take the lock back, unless a new runner beat",
            "        # us to it -- in which case that one will dispatch it.",
            "        try { New-Item -ItemType Directory -Path 'lock' -ErrorAction Stop "
            "| Out-Null } catch { exit 0 }",
            "        Set-Content -Path 'lock\\pid' -Value $PID -Encoding ascii",
            "        continue",
            "    }",
            f"    Start-Sleep -Seconds {poll}",
            "}",
            "",
        ]
    )


def prepare_command(directory: str) -> str:
    """Create the runner's directories. Safe to repeat."""
    quoted = ps_quote(directory)
    names = ",".join(ps_quote(name) for name in SUBDIRS)
    return (
        f"New-Item -ItemType Directory -Force -Path {quoted} | Out-Null; "
        f"Set-Location -LiteralPath {quoted}; "
        f"foreach ($d in @({names})) {{ "
        "New-Item -ItemType Directory -Force -Path $d | Out-Null }"
    )


def list_command(directory: str) -> str:
    """Every entry the runner knows about, as ``<state> <entry>`` lines."""
    quoted = ps_quote(directory)
    return (
        f"if (-not (Test-Path -LiteralPath {quoted})) {{ exit 0 }}; "
        f"Set-Location -LiteralPath {quoted}; "
        "foreach ($d in @('queue','running','done')) { "
        "foreach ($f in @(Get-ChildItem -LiteralPath $d -File "
        '-ErrorAction SilentlyContinue)) { "$d $($f.Name)" } }'
    )


def enqueue_command(directory: str, entry: str) -> str:
    """Move an uploaded script from ``tmp\\`` into ``queue\\``."""
    quoted = ps_quote(directory)
    return (
        f"Set-Location -LiteralPath {quoted}; "
        f"Move-Item -LiteralPath {ps_quote('tmp\\' + entry)} "
        f"-Destination {ps_quote('queue\\' + entry)} -Force"
    )


def ensure_runner_command(directory: str, script_name: str = RUNNER_SCRIPT_NAME) -> str:
    """One command that guarantees a runner is up, and is safe to repeat.

    Run *after* the job is in the queue, never before: a runner started first
    can empty the queue and exit before the job arrives.
    """
    quoted = ps_quote(directory)
    return (
        f"Set-Location -LiteralPath {quoted}; "
        # Reclaim a lock left behind by a runner that is no longer alive.
        "if (Test-Path -LiteralPath 'lock') { "
        "$p = Get-Content -LiteralPath 'lock\\pid' -ErrorAction SilentlyContinue | "
        "Select-Object -First 1; "
        "if (-not ($p -match '^\\d+$') -or "
        "-not (Get-Process -Id ([int]$p) -ErrorAction SilentlyContinue)) { "
        "Remove-Item -LiteralPath 'lock' -Recurse -Force -ErrorAction SilentlyContinue } }; "
        "try { New-Item -ItemType Directory -Path 'lock' -ErrorAction Stop | Out-Null } "
        "catch { 'running'; exit 0 }; "
        # Absolute, and with an explicit working directory: Start-Process
        # resolves relative paths against PowerShell's location in pwsh 7 and
        # against the process's working directory in 5.1.
        f"$proc = Start-Process -FilePath powershell -ArgumentList {_PS_ARGS},"
        f"{ps_quote(_join(directory, script_name))} "
        f"-WorkingDirectory {quoted} -WindowStyle Hidden -PassThru "
        f"-RedirectStandardOutput {ps_quote(_join(directory, RUNNER_LOG_NAME))} "
        f"-RedirectStandardError {ps_quote(_join(directory, RUNNER_LOG_NAME + '.err'))}; "
        "Set-Content -Path 'lock\\pid' -Value $proc.Id -Encoding ascii; "
        "'started'"
    )


def cancel_command(directory: str, entry: str) -> str:
    """Cancel whether the job is waiting or already running."""
    quoted = ps_quote(directory)
    return (
        f"Set-Location -LiteralPath {quoted}; "
        f"try {{ Move-Item -LiteralPath {ps_quote('queue\\' + entry)} "
        f"-Destination {ps_quote('done\\' + entry)} -ErrorAction Stop; 'dequeued'; exit 0 }} "
        "catch { }; "
        f"$p = Get-Content -LiteralPath {ps_quote('pids\\' + entry)} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if (-not ($p -match '^\\d+$')) { exit 0 }; "
        # /T for the tree: the queued script is a PowerShell host that started
        # the wrapper as a child, which started the payload as its own.
        "& taskkill /PID $p /T /F"
    )


def set_slots_command(directory: str, slots: int) -> str:
    """Change the job limit under a running runner; it re-reads it each pass."""
    path = ps_quote(_join(directory, SLOTS_NAME))
    return f"Set-Content -Path {path} -Value {max(1, int(slots))} -Encoding ascii"


def set_cores_command(directory: str, cores: int) -> str:
    """Change how many cores the runner may hand out. 0 restores the default."""
    path = ps_quote(_join(directory, CORES_NAME))
    value = max(0, int(cores))
    if not value:
        return f"Remove-Item -LiteralPath {path} -Force -ErrorAction SilentlyContinue"
    return f"Set-Content -Path {path} -Value {value} -Encoding ascii"


def pause_command(directory: str, paused: bool) -> str:
    """Hold the queue, or let it move again. Running jobs are untouched."""
    path = ps_quote(_join(directory, PAUSED_NAME))
    if paused:
        return f"New-Item -ItemType File -Force -Path {path} | Out-Null"
    return f"Remove-Item -LiteralPath {path} -Force -ErrorAction SilentlyContinue"


__all__: List[str] = [
    "ENTRY_SUFFIX",
    "RUNNER_SCRIPT_NAME",
    "build_job_script",
    "build_runner_script",
    "cancel_command",
    "enqueue_command",
    "ensure_runner_command",
    "list_command",
    "pause_command",
    "prepare_command",
    "set_cores_command",
    "set_slots_command",
]
