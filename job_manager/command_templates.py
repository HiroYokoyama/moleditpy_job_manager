"""Ready-made command lines, one per program MoleditPy can write input for.

The command is the one thing the plugin cannot guess: every site spells its
launcher differently. These are the conventional invocations, offered as a
starting point that the user edits -- never applied on top of a preset the user
has already saved.

Each entry lists the extensions the matching input generator writes, so the
wizard can put the likely one first: ORCA Input Generator Pro saves ``.inp``,
Gaussian ``.gjf``/``.com``, NWChem ``.nw``, Psi4 ``.dat``/``.in``, PySCF a
``.py`` script, and so on.

Placeholders are the ones :func:`job_manager.schedulers.base.format_command`
substitutes: ``{input}``, ``{stem}``, ``{basename}``, ``{name}``, ``{jobdir}``,
``{nodes}``, ``{ntasks}``, ``{cpus}``, ``{memory}``, ``{queue}``, ``{walltime}``.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class CommandTemplate:
    """One program's conventional command line."""

    label: str
    command: str
    #: Input extensions the matching generator writes, lowercase with the dot.
    extensions: Tuple[str, ...] = ()
    note: str = ""
    #: What is worth bringing back from this program's job directory. Empty
    #: means "no opinion", and the wizard's own default list is left alone.
    #: Each is what the program actually writes -- Gaussian's .log and .chk,
    #: ORCA's .gbw and .hess -- rather than a guess at what a user wants,
    #: because a pattern that matches nothing downloads nothing and says
    #: nothing about why.
    fetch_globs: Tuple[str, ...] = ()


#: Ordered for the dropdown: the two Pro generators first, then the rest
#: alphabetically, then the periodic codes, then the escape hatch.
TEMPLATES: Tuple[CommandTemplate, ...] = (
    CommandTemplate(
        "ORCA",
        "$(which orca) {input} > {stem}.out",
        (".inp",),
        "ORCA needs its own absolute path to start MPI workers, which is what "
        "$(which orca) supplies; a bare 'orca' runs serial only.",
        fetch_globs=("*.out", "*.xyz", "*.hess", "*.gbw", "*.trj", "*.engrad"),
    ),
    CommandTemplate(
        "Gaussian 16",
        "g16 {input}",
        (".gjf", ".com"),
        "g16 writes the .log itself, so no redirection is needed.",
        fetch_globs=("*.log", "*.chk", "*.fchk"),
    ),
    CommandTemplate(
        "Gaussian 09",
        "g09 {input}",
        (".gjf", ".com"),
        fetch_globs=("*.log", "*.chk", "*.fchk"),
    ),
    CommandTemplate(
        "CP2K",
        "mpirun -np {ntasks} cp2k.psmp -i {input} -o {stem}.out",
        (".inp",),
        fetch_globs=("*.out", "*.xyz", "*.restart", "*.ener"),
    ),
    CommandTemplate(
        "GAMESS (US)",
        "rungms {input} 00 {ntasks} > {stem}.log",
        (".inp", ".src"),
        "rungms takes the version tag and the core count as positional arguments.",
        fetch_globs=("*.log", "*.dat", "*.trj"),
    ),
    CommandTemplate(
        "MOPAC",
        "mopac {input}",
        (".mop", ".dat"),
        fetch_globs=("*.out", "*.arc", "*.aux"),
    ),
    CommandTemplate(
        "NWChem",
        "mpirun -np {ntasks} nwchem {input} > {stem}.out",
        (".nw",),
        fetch_globs=("*.out", "*.movecs", "*.hess", "*.xyz"),
    ),
    CommandTemplate(
        "Psi4",
        "psi4 -i {input} -o {stem}.out -n {cpus}",
        (".dat", ".in"),
        fetch_globs=("*.out", "*.molden", "*.fchk"),
    ),
    CommandTemplate(
        "PySCF (Python script)",
        "python {input} > {stem}.out",
        (".py",),
        fetch_globs=("*.out", "*.chk", "*.molden"),
    ),
    CommandTemplate(
        "Quantum ESPRESSO (pw.x)",
        "mpirun -np {ntasks} pw.x -in {input} > {stem}.out",
        (".in",),
        fetch_globs=("*.out", "*.xml"),
    ),
    CommandTemplate(
        "VASP",
        "mpirun -np {ntasks} vasp_std > vasp.out",
        (),
        "VASP reads INCAR, POSCAR, KPOINTS and POTCAR from the working "
        "directory and takes no input filename -- select all of them as the "
        "job's input files.",
        fetch_globs=("OUTCAR", "CONTCAR", "OSZICAR", "vasprun.xml", "XDATCAR", "*.out"),
    ),
    CommandTemplate(
        "xTB (optimisation)",
        "xtb {input} --opt > {stem}.out",
        (".xyz",),
        fetch_globs=("*.out", "xtbopt.xyz", "xtbopt.log", "g98.out"),
    ),
    CommandTemplate(
        "Custom (type your own)",
        "",
        (),
        "srun works in place of 'mpirun -np {ntasks}' on most SLURM sites.",
    ),
)


def extension_of(filename: str) -> str:
    """Lowercase extension of a remote or local file name, dot included."""
    return posixpath.splitext(filename or "")[1].lower()


def templates_for(filename: str = "") -> List[CommandTemplate]:
    """Every template, with the ones matching ``filename`` first."""
    extension = extension_of(filename)
    if not extension:
        return list(TEMPLATES)
    matching = [t for t in TEMPLATES if extension in t.extensions]
    return matching + [t for t in TEMPLATES if t not in matching]


def suggest(filename: str) -> Optional[CommandTemplate]:
    """The single most likely template for a file, or None when ambiguous.

    ``.inp`` is written by ORCA, CP2K *and* GAMESS, so it is deliberately not
    guessed: picking one of three silently would be worse than picking none.
    """
    extension = extension_of(filename)
    if not extension:
        return None
    matching = [t for t in TEMPLATES if extension in t.extensions]
    return matching[0] if len(matching) == 1 else None


__all__ = ["CommandTemplate", "TEMPLATES", "extension_of", "suggest", "templates_for"]
