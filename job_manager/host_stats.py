"""What a host is doing right now: load, memory, and how many cores it has.

One command, printing ``key=value`` lines, so the parser never has to care
which of the three shapes the host answered in. Nothing here raises for a
missing field: a machine that does not answer about its memory is common
(macOS has no /proc), and it must not cost the reading of its load.

Deliberately cheap, and deliberately not part of polling: this runs only while
the host panel is open, because a load average is worth one command every few
seconds when someone is watching and worth nothing at all when nobody is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .remote_runner import CORE_COUNT_SH

#: POSIX. /proc first because it is exact, then the portable fallbacks: uptime
#: prints a load average on every Unix, and sysctl answers on macOS and BSD.
#: Each is guarded so a missing source prints nothing rather than an error.
POSIX_COMMAND = (
    # Physical cores, not hardware threads, and by the same means the helper
    # queue counts them with -- `nproc` reports logical processors, so a
    # six-core machine calls itself twelve and a load of 6 then reads as half
    # a machine when it is a full one.
    f"{CORE_COUNT_SH}; echo cores=$c; "
    "echo threads=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null); "
    "inst=\"\"; "
    "if [ -r /proc/stat ]; then "
    "t1=$(awk '/^cpu /{print $2+$3+$4+$7+$8+$9, $2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat 2>/dev/null); "
    "sleep 0.05 2>/dev/null || true; "
    "t2=$(awk '/^cpu /{print $2+$3+$4+$7+$8+$9, $2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat 2>/dev/null); "
    "u1=${t1%% *}; tt1=${t1##* }; "
    "u2=${t2%% *}; tt2=${t2##* }; "
    "du=$((u2 - u1)); dt=$((tt2 - tt1)); "
    "if [ \"$dt\" -gt 0 ] 2>/dev/null && [ \"$c\" -gt 0 ] 2>/dev/null; then "
    "inst=$(awk -v du=\"$du\" -v dt=\"$dt\" -v c=\"$c\" 'BEGIN {printf \"%.2f\", (du/dt)*c}' 2>/dev/null); "
    "fi; "
    "fi; "
    "if [ -n \"$inst\" ]; then "
    "echo load=$inst $(cut -d' ' -f2-3 /proc/loadavg 2>/dev/null); "
    "elif [ -r /proc/loadavg ]; then "
    "echo load=$(cut -d' ' -f1-3 /proc/loadavg); "
    "else "
    "echo load=$(uptime 2>/dev/null | sed -n 's/.*load averages*:[ ]*//p' | tr -d ','); "
    "fi; "
    "if [ -r /proc/meminfo ]; then "
    'awk \'/^MemTotal:/{printf "mem_total=%d\\n", int($2/1024)} '
    '/^MemAvailable:/{printf "mem_free=%d\\n", int($2/1024)}\' /proc/meminfo; '
    "elif command -v sysctl >/dev/null 2>&1; then "
    "echo mem_total=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1048576 )); "
    "fi"
)

#: PowerShell, for the native Windows host. TotalVisibleMemorySize and
#: FreePhysicalMemory are in KB; LoadPercentage is a percentage, which is not a
#: load average -- it is scaled to the core count so the two read alike.
POWERSHELL_COMMAND = (
    "$os = Get-CimInstance Win32_OperatingSystem; "
    "$cpu = Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage "
    "-Average; "
    "$cores = (Get-CimInstance Win32_Processor | "
    "Measure-Object -Property NumberOfCores -Sum).Sum; "
    'Write-Output "cores=$cores"; '
    'Write-Output "mem_total=$([int]($os.TotalVisibleMemorySize/1024))"; '
    'Write-Output "mem_free=$([int]($os.FreePhysicalMemory/1024))"; '
    'Write-Output "load=$([math]::Round($cpu.Average * $cores / 100, 2))"'
)


@dataclass
class HostStats:
    """One sample. Every field is optional because every source can be absent."""

    cores: int = 0
    #: Hardware threads, which is what `nproc` counts. Reported separately so a
    #: user who knows the machine as "12 threads" sees why the bar says 6.
    threads: int = 0
    #: One, five and fifteen minute load averages, as the host reported them.
    load: tuple = ()
    mem_total_mb: int = 0
    mem_free_mb: int = 0
    #: Empty when the sample was taken; otherwise why it was not.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def mem_used_mb(self) -> int:
        if not self.mem_total_mb or not self.mem_free_mb:
            return 0
        return max(0, self.mem_total_mb - self.mem_free_mb)

    @property
    def memory_fraction(self) -> float:
        """Used memory as 0..1, or 0 where the host did not say."""
        if not self.mem_total_mb:
            return 0.0
        return min(1.0, self.mem_used_mb / self.mem_total_mb)

    @property
    def load_fraction(self) -> float:
        """One-minute load against the core count, clamped to 0..1.

        A load equal to the core count is a full machine, which is the point
        the bar should be full at -- not 100 on some arbitrary scale. Above
        that it stays full and the number beside it tells the rest.
        """
        if not self.cores or not self.load:
            return 0.0
        return min(1.0, max(0.0, self.load[0] / self.cores))

    @property
    def summary(self) -> str:
        """One line, for the panel and for a tooltip."""
        if self.error:
            return self.error
        parts = []
        if self.load:
            parts.append("load " + " ".join(f"{value:.2f}" for value in self.load))
        if self.cores:
            cores = f"{self.cores} cores"
            if self.threads > self.cores:
                cores += f" ({self.threads} threads)"
            parts.append(cores)
        if self.mem_total_mb and self.mem_free_mb:
            parts.append(f"{self.mem_used_mb / 1024:.1f}/{self.mem_total_mb / 1024:.1f} GB")
        elif self.mem_total_mb:
            # Used is unknown on a host with no MemAvailable (macOS, and some
            # containers). Saying "0.0 of 15.6 GB" would be a made-up number.
            parts.append(f"{self.mem_total_mb / 1024:.1f} GB total")
        return ", ".join(parts) or "no answer"


def command_for(powershell: bool) -> str:
    return POWERSHELL_COMMAND if powershell else POSIX_COMMAND


def _first_number(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse(text: str) -> HostStats:
    """Read the key=value lines, ignoring anything else on the wire.

    A login banner, a stray warning from a dotfile, a blank line: all of it is
    skipped rather than failing the sample. What the host did not say is left
    at zero, and the caller shows a dash for it.
    """
    stats = HostStats()
    for line in (text or "").splitlines():
        key, _, value = line.strip().partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "cores":
            number = _first_number(value)
            stats.cores = int(number) if number else 0
        elif key == "load":
            numbers = [_first_number(part) for part in value.replace(",", " ").split()]
            stats.load = tuple(n for n in numbers if n is not None)[:3]
        elif key == "threads":
            number = _first_number(value)
            stats.threads = int(number) if number else 0
        elif key == "mem_total":
            number = _first_number(value)
            stats.mem_total_mb = int(number) if number else 0
        elif key == "mem_free":
            number = _first_number(value)
            stats.mem_free_mb = int(number) if number else 0
    return stats


__all__ = [
    "POSIX_COMMAND",
    "POWERSHELL_COMMAND",
    "HostStats",
    "command_for",
    "parse",
]
