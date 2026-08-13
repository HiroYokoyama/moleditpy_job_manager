"""Read the resources an input file already asks for.

Every quantum chemistry input states its own memory and core request, and the
user has just typed those numbers once. Asking them to type them again into the
submit wizard -- so that the helper queue knows what to reserve -- is asking
them to keep two copies of one fact in step, and the copy the scheduler uses is
the one they will forget.

So the wizard reads the input instead. What comes back is a *request*, not a
measurement: it is what the program was told to use, which is exactly what a
queue needs to decide what can run alongside what.

**ORCA states memory per core**, and that is the trap in this file. ``%maxcore
3000`` with ``%pal nprocs 8`` is a 24 GB job, not a 3 GB one. Reading it as a
total would let three of them onto a 32 GB machine and the operating system
would kill two of them hours later.

Nothing here is guessed. A format that does not state a resource returns 0 for
it, and 0 means "no request" everywhere downstream -- a job that asks for
nothing waits for nothing, which is the safe direction when the alternative is
inventing a number.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

#: Enough of a file to hold any header block, and small enough that scanning a
#: directory of inputs costs nothing. Every directive here is in the preamble.
MAX_BYTES = 200_000


class Resources(NamedTuple):
    """What an input asks for. 0 means it did not say."""

    memory_mb: int = 0
    cores: int = 0
    #: Which format was recognised, for the line shown to the user.
    program: str = ""

    @property
    def found(self) -> bool:
        return bool(self.memory_mb or self.cores)


def _int(text: str) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


#: Bytes per unit. Quantum chemistry sizes memory in *words* of eight bytes as
#: often as in bytes, and a unit-less figure in a Gaussian ``%mem`` is words --
#: so ``%mem=1000000`` is 7.6 MB, not a gigabyte. Reading it as the latter
#: would have the job reserve a machine it never needed.
_BYTES_PER_UNIT = {
    "": 8,
    "W": 8,
    "WORD": 8,
    "WORDS": 8,
    "KW": 8 * 1024,
    "MW": 8 * 1024**2,
    "GW": 8 * 1024**3,
    "B": 1,
    "KB": 1024,
    "K": 1024,
    "MB": 1024**2,
    "M": 1024**2,
    "GB": 1024**3,
    "G": 1024**3,
    "TB": 1024**4,
    "T": 1024**4,
}


def _unit_to_mb(value: float, unit: str) -> int:
    """A size with a unit, in megabytes. 0 for a unit that is not one."""
    per_unit = _BYTES_PER_UNIT.get(unit.strip().upper())
    if per_unit is None:
        return 0
    return int(value * per_unit / (1024 * 1024))


# --- ORCA -------------------------------------------------------------------

_ORCA_MAXCORE = re.compile(r"^\s*%\s*maxcore\s+(\d+)", re.IGNORECASE | re.MULTILINE)
_ORCA_NPROCS = re.compile(r"%\s*pal\b.*?nprocs\s+(\d+)", re.IGNORECASE | re.DOTALL)
_ORCA_PAL_BANG = re.compile(r"^\s*!.*?\bpal(\d+)\b", re.IGNORECASE | re.MULTILINE)


def _orca(text: str) -> Resources:
    maxcore = _ORCA_MAXCORE.search(text)
    nprocs = _ORCA_NPROCS.search(text)
    if not nprocs:
        nprocs = _ORCA_PAL_BANG.search(text)
    if not maxcore and not nprocs:
        return Resources()
    cores = _int(nprocs.group(1)) if nprocs else 0
    # Per core, not total. The whole reason this module documents itself.
    per_core = _int(maxcore.group(1)) if maxcore else 0
    return Resources(per_core * max(1, cores), cores, "ORCA")


# --- Gaussian ---------------------------------------------------------------

_GAUSSIAN_MEM = re.compile(r"^\s*%\s*mem\s*=\s*(\d+(?:\.\d+)?)\s*([a-z]*)", re.IGNORECASE | re.M)
_GAUSSIAN_NPROC = re.compile(r"^\s*%\s*nproc(?:shared|linda)?\s*=\s*(\d+)", re.IGNORECASE | re.M)


def _gaussian(text: str) -> Resources:
    memory = _GAUSSIAN_MEM.search(text)
    nproc = _GAUSSIAN_NPROC.search(text)
    if not memory and not nproc:
        return Resources()
    # Gaussian's %mem is the total for the job, and its default unit is plain
    # words -- not megawords, which is the mistake that turns 7 MB into 8 TB.
    total = _unit_to_mb(float(memory.group(1)), memory.group(2) or "") if memory else 0
    return Resources(total, _int(nproc.group(1)) if nproc else 0, "Gaussian")


# --- the rest ---------------------------------------------------------------

_PSI4_MEM = re.compile(r"^\s*memory\s+(\d+(?:\.\d+)?)\s*([a-z]+)", re.IGNORECASE | re.MULTILINE)
_PSI4_MARK = re.compile(r"\b(psi4|set_num_threads|energy\(|optimize\()", re.IGNORECASE)


def _psi4(text: str) -> Resources:
    memory = _PSI4_MEM.search(text)
    if not memory or not _PSI4_MARK.search(text):
        return Resources()
    return Resources(_unit_to_mb(float(memory.group(1)), memory.group(2)), 0, "Psi4")


_NWCHEM_MEM = re.compile(
    r"^\s*memory\s+(?:total\s+)?(\d+(?:\.\d+)?)\s*([a-z]+)", re.IGNORECASE | re.MULTILINE
)
_NWCHEM_MARK = re.compile(r"^\s*(geometry|task\s+\w+|start\s+\w+)", re.IGNORECASE | re.MULTILINE)


def _nwchem(text: str) -> Resources:
    memory = _NWCHEM_MEM.search(text)
    if not memory or not _NWCHEM_MARK.search(text):
        return Resources()
    return Resources(_unit_to_mb(float(memory.group(1)), memory.group(2)), 0, "NWChem")


_QCHEM_MEM = re.compile(r"^\s*mem_total\s+(\d+)", re.IGNORECASE | re.MULTILINE)


def _qchem(text: str) -> Resources:
    memory = _QCHEM_MEM.search(text)
    if not memory or "$rem" not in text.lower():
        return Resources()
    return Resources(_int(memory.group(1)), 0, "Q-Chem")


_GAMESS_MWORDS = re.compile(r"\bmwords\s*=\s*(\d+)", re.IGNORECASE)
_GAMESS_MARK = re.compile(r"\$(system|contrl)\b", re.IGNORECASE)


def _gamess(text: str) -> Resources:
    words = _GAMESS_MWORDS.search(text)
    if not words or not _GAMESS_MARK.search(text):
        return Resources()
    # MWORDS is per core, in millions of 8-byte words.
    return Resources(_int(words.group(1)) * 8, 0, "GAMESS")


#: Order matters only in that the more distinctive formats are tried first;
#: each reader insists on a marker of its own format before claiming a file.
_READERS = (_orca, _gaussian, _qchem, _gamess, _nwchem, _psi4)


def scan_text(text: str) -> Resources:
    """The resources stated in an input file's text."""
    for reader in _READERS:
        try:
            found = reader(text)
        except (ValueError, TypeError):  # a malformed number is not a crash
            logging.debug("Job Manager: input scan failed", exc_info=True)
            continue
        if found.found:
            return found
    return Resources()


def scan(path: str) -> Resources:
    """The resources stated in an input file. Empty for anything unreadable."""
    try:
        # Only the front of the file: every directive here is in the preamble,
        # and a trajectory should not be read into memory to learn it says
        # nothing.
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_BYTES)
    except OSError:
        logging.debug("Job Manager: could not read %s for a resource scan", path, exc_info=True)
        return Resources()
    return scan_text(text)


def format_memory(memory_mb: int) -> str:
    """Back to the spelling a user writes: 8192 -> ``8G``."""
    value = max(0, int(memory_mb or 0))
    if not value:
        return ""
    if value % 1024 == 0:
        return f"{value // 1024}G"
    return f"{value}M"


__all__ = ["MAX_BYTES", "Resources", "format_memory", "scan", "scan_text"]
