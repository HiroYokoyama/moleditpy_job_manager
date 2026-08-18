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
    DIGEST_NAME,
    MEMORY_NAME,
    MEMORY_TAG,
    PAUSED_NAME,
    REQUIRE_SUCCESS_TAG,
    RUNNER_LOG_NAME,
    RUNNER_POLL_SECONDS,
    SEQUENCE_NAME,
    SLOTS_NAME,
    STATUS_BLOCKED,
    SUBDIRS,
)
from .schedulers.windows import ps_quote

#: The unversioned name, as in the bash flavour: kept for the harnesses and
#: for a runner left by an older plugin. What is written and started is
#: :func:`runner_script_name`.
RUNNER_SCRIPT_NAME = "moleditpy_runner.ps1"
#: Queue entries are PowerShell scripts here.
ENTRY_SUFFIX = ".ps1"

#: How the runner starts a job, and how the plugin starts the runner.
_PS_ARGS = "'-NoProfile','-ExecutionPolicy','Bypass','-File'"
#: PowerShell 7 must launch its children with pwsh; Windows PowerShell uses
#: powershell.exe. Resolve from the current interpreter so a host that selects
#: one never silently starts the other.
_PS_SHELL = (
    "if ($PSEdition -eq 'Core') { Join-Path $PSHOME 'pwsh.exe' } "
    "else { Join-Path $PSHOME 'powershell.exe' }"
)


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
    memory_mb: int = 0,
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
    if int(memory_mb or 0) > 0:
        lines.append(f"{MEMORY_TAG} {int(memory_mb)}")
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
        f"$__moleditpy_shell = {_PS_SHELL}",
        "$__moleditpy_p = Start-Process -FilePath $__moleditpy_shell -ArgumentList "
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
    # Fractional allowed: production passes 5, and the tests that drive a real
    # runner would otherwise pay a whole second per dispatch.
    poll = max(0.1, float(poll_seconds))
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
            f"$__moleditpy_shell = {_PS_SHELL}",
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
            # Physical cores, not hardware threads: ProcessorCount counts the
            # latter, and a budget of twelve on a six-core machine lets two
            # six-core jobs thrash each other.
            f"    {_PS_CORE_COUNT}",
            "    if ($c -lt 1) { $c = 1 }",
            f"    return (Read-Limit {ps_quote(CORES_NAME)} $c)",
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
            "function Get-TotalMemory {",
            # Not Read-Limit: an explicit 0 in the file means "do not schedule
            # on memory", and Read-Limit treats 0 as absent and would fall back
            # to the machine's total. The bash flavour honours a written 0, and
            # the two must not disagree about what the same file means.
            f"    $v = Get-Content -LiteralPath {ps_quote(MEMORY_NAME)} "
            "-ErrorAction SilentlyContinue | Select-Object -First 1",
            "    if ($v -match '^\\d+$') { return [int]$v }",
            "    $mb = 0",
            "    try {",
            "        $bytes = (Get-CimInstance -ClassName Win32_ComputerSystem "
            "-ErrorAction Stop).TotalPhysicalMemory",
            "        if ($bytes) { $mb = [int]([math]::Floor($bytes / 1MB)) }",
            "    } catch { $mb = 0 }",
            "    return $mb",
            "}",
            "",
            "# 0 when the job asked for none, which is what a blank field means.",
            "function Get-JobMemory($path) {",
            f"    $v = Get-Header $path {ps_quote(MEMORY_TAG)}",
            "    if ($v -match '^\\d+$') { return [int]$v }",
            "    return 0",
            "}",
            "",
            "function Get-UsedMemory {",
            "    $total = 0",
            "    foreach ($f in @(Get-ChildItem -LiteralPath 'running' -File "
            "-ErrorAction SilentlyContinue)) {",
            "        $total += (Get-JobMemory $f.FullName)",
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
            "    $memcap = Get-TotalMemory",
            # Sorted on the number itself, not as text: past 9999 the padding
            # runs out and job_10000 sorts before job_9999, inverting the
            # dispatch order exactly when a queue has been busy a long time.
            "    foreach ($f in @(Get-ChildItem -LiteralPath 'queue' -File "
            "-ErrorAction SilentlyContinue | "
            "Sort-Object @{Expression={[int](($_.Name -split '_')[1])}}, Name)) {",
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
            "        $wantmem = Get-JobMemory ('queue\\' + $entry)",
            "        if ($memcap -gt 0 -and $wantmem -gt $memcap) { $wantmem = $memcap }",
            "        # Memory is a second budget, for the same reason: two jobs of",
            "        # 90G on a 120G machine must not both start because the cores",
            "        # were free. Overcommitting memory kills a job hours in.",
            "        if ($memcap -gt 0 -and ((Get-UsedMemory) + $wantmem) -gt $memcap) {",
            "            break",
            "        }",
            "        # Claiming a job *is* moving it: Move-Item fails when the",
            "        # destination exists, so two runners cannot both win.",
            "        try {",
            "            Move-Item -LiteralPath ('queue\\' + $entry) "
            "-Destination ('running\\' + $entry) -ErrorAction Stop",
            "        } catch { continue }",
            "        $proc = Start-Process -FilePath $__moleditpy_shell -ArgumentList "
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
            f"    Start-Sleep -Seconds {poll:g}",
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


def claim_sequence_command(directory: str) -> str:
    """Take the next dispatch number, and print it. Never goes backwards.

    Same rule as the bash flavour: the highest ever issued is kept on the host,
    because deriving the number from the queue alone restarts it when a user
    clears ``done/`` -- and a new job then sorts ahead of everything waiting.
    """
    quoted = ps_quote(directory)
    counter = ps_quote(_join(directory, SEQUENCE_NAME))
    temp = ps_quote(_join(directory, SEQUENCE_NAME + ".tmp"))
    return (
        f"Set-Location -LiteralPath {quoted}; "
        f"$v = Get-Content -LiteralPath {counter} -ErrorAction SilentlyContinue | "
        "Select-Object -First 1; "
        r"$n = 0; if ($v -match '^\d+$') { $n = [int]$v }; "
        "foreach ($d in @('queue','running','done')) { "
        "foreach ($f in @(Get-ChildItem -LiteralPath $d -File "
        "-ErrorAction SilentlyContinue)) { "
        "$m = ($f.Name -split '_')[1]; "
        r"if ($m -match '^\d+$' -and [int]$m -gt $n) { $n = [int]$m } } }; "
        "$n = $n + 1; "
        f"Set-Content -Path {temp} -Value $n -Encoding ascii; "
        f"Move-Item -LiteralPath {temp} -Destination {counter} -Force; "
        "$n"
    )


def runner_script_name(digest: str) -> str:
    """The runner script's file name for one version of its contents.

    Content-addressed for the same reasons as the bash flavour: a runner that
    is up holds its script open, and a script that ran a job is worth keeping.
    """
    return f"moleditpy_runner_{digest}.ps1"


def setup_command(directory: str, slots: int, cores: int, memory_mb: int = 0) -> str:
    """Prepare, set the limits, and report the runner script already there."""
    digest_path = ps_quote(_join(directory, DIGEST_NAME))
    prefix = ps_quote(_join(directory, "moleditpy_runner_"))
    parts = [
        prepare_command(directory),
        set_slots_command(directory, slots),
        set_cores_command(directory, cores),
        set_memory_command(directory, memory_mb),
        # Reported only when the script that digest names is really still
        # there: a version whose file has been deleted would have the caller
        # skip the upload and then start a runner that does not exist.
        f"$d = if (Test-Path -LiteralPath {digest_path}) "
        f"{{ Get-Content -LiteralPath {digest_path} | Select-Object -First 1 }} else {{ '' }}",
        f"if ($d -and (Test-Path -LiteralPath ({prefix} + $d + '.ps1'))) {{ $d }}",
    ]
    return "; ".join(parts)


def store_digest_command(directory: str, digest: str) -> str:
    """Record which runner script is on the host, after uploading it."""
    path = ps_quote(_join(directory, DIGEST_NAME))
    return f"Set-Content -Path {path} -Value {ps_quote(digest)} -Encoding ascii"


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
    """Move an uploaded script from ``tmp`` into ``queue``."""
    # Built before the f-strings, not inside them: a backslash in an f-string
    # *expression* is a syntax error before Python 3.12, and this plugin
    # supports 3.9 upwards -- so the module would not even import for most of
    # its users, which is exactly how CI found this.
    source = ps_quote(_join("tmp", entry))
    target = ps_quote(_join("queue", entry))
    return (
        f"Set-Location -LiteralPath {ps_quote(directory)}; "
        f"Move-Item -LiteralPath {source} -Destination {target} -Force"
    )


def ensure_runner_command(directory: str, script_name: str) -> str:
    """One command that guarantees a runner is up, and is safe to repeat.

    Run *after* the job is in the queue, never before: a runner started first
    can empty the queue and exit before the job arrives.
    """
    quoted = ps_quote(directory)
    return (
        f"Set-Location -LiteralPath {quoted}; "
        # The script has to be there, or "started" would be a lie and the queue
        # would simply never move.
        f"if (-not (Test-Path -LiteralPath {ps_quote(script_name)})) "
        "{ 'missing'; exit 1 }; "
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
        f"$__moleditpy_shell = {_PS_SHELL}; "
        f"$proc = Start-Process -FilePath $__moleditpy_shell -ArgumentList {_PS_ARGS},"
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
    # Outside the f-strings; see enqueue_command.
    queued = ps_quote(_join("queue", entry))
    finished = ps_quote(_join("done", entry))
    pid_file = ps_quote(_join("pids", entry))
    return (
        f"Set-Location -LiteralPath {quoted}; "
        f"try {{ Move-Item -LiteralPath {queued} "
        f"-Destination {finished} -ErrorAction Stop; 'dequeued'; exit 0 }} "
        "catch { }; "
        f"$p = Get-Content -LiteralPath {pid_file} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if (-not ($p -match '^\\d+$')) { exit 0 }; "
        # /T for the tree: the queued script is a PowerShell host that started
        # the wrapper as a child, which started the payload as its own.
        "& taskkill /PID $p /T /F"
    )


def release_command(directory: str, entry: str) -> str:
    """The PowerShell half of :func:`remote_runner.release_command`."""
    quoted = ps_quote(directory)
    queued = ps_quote(_join("queue", entry))
    tag = ps_quote(REQUIRE_SUCCESS_TAG)
    return (
        f"Set-Location -LiteralPath {quoted}; "
        f"if (-not (Test-Path -LiteralPath {queued})) {{ exit 0 }}; "
        f"$lines = Get-Content -LiteralPath {queued}; "
        f"$lines = $lines | ForEach-Object {{ if ($_ -like ({tag} + '*')) "
        f"{{ {tag} + ' 0' }} else {{ $_ }} }}; "
        f"Set-Content -LiteralPath {queued} -Value $lines -Encoding ascii; "
        "'released'"
    )


def set_slots_command(directory: str, slots: int) -> str:
    """Change the job limit under a running runner; it re-reads it each pass."""
    path = ps_quote(_join(directory, SLOTS_NAME))
    return f"Set-Content -Path {path} -Value {max(1, int(slots))} -Encoding ascii"


#: Physical cores, falling back to logical processors. ProcessorCount counts
#: hardware threads, so on a hyperthreaded machine it is double the number of
#: cores a calculation can actually use.
_PS_CORE_COUNT = (
    "$t = [Environment]::ProcessorCount; $c = 0; "
    "try { $c = [int]((Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop "
    "| Measure-Object -Property NumberOfCores -Sum).Sum) } catch { $c = 0 }; "
    "if ($c -lt 1) { $c = $t }"
)


def probe_command() -> str:
    """The host's cores, threads and total memory, spelled as bash spells it."""
    return (
        f"{_PS_CORE_COUNT}; $m = 0; "
        "try { $b = (Get-CimInstance -ClassName Win32_ComputerSystem "
        "-ErrorAction Stop).TotalPhysicalMemory; "
        "if ($b) { $m = [int]([math]::Floor($b / 1MB)) } } catch { $m = 0 }; "
        '"cores=$c threads=$t memory=$m"'
    )


def set_memory_command(directory: str, memory_mb: int) -> str:
    """Change the memory budget, in MB. 0 restores the machine's own total."""
    path = ps_quote(_join(directory, MEMORY_NAME))
    value = max(0, int(memory_mb or 0))
    if not value:
        return f"Remove-Item -LiteralPath {path} -Force -ErrorAction SilentlyContinue"
    return f"Set-Content -Path {path} -Value {value} -Encoding ascii"


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


def is_paused_command(directory: str) -> str:
    """Prints ``paused`` or ``running``. Same two words as the bash flavour."""
    path = ps_quote(_join(directory, PAUSED_NAME))
    return f"if (Test-Path -LiteralPath {path}) {{ 'paused' }} else {{ 'running' }}"


__all__: List[str] = [
    "ENTRY_SUFFIX",
    "RUNNER_SCRIPT_NAME",
    "build_job_script",
    "build_runner_script",
    "cancel_command",
    "enqueue_command",
    "ensure_runner_command",
    "is_paused_command",
    "list_command",
    "pause_command",
    "prepare_command",
    "release_command",
    "set_cores_command",
    "set_memory_command",
    "set_slots_command",
    "setup_command",
    "store_digest_command",
]
