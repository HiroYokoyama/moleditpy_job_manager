"""Submission labels, file filters and command-selector action identities."""

INPUT_FILTERS = (
    "Calculation inputs (*.inp *.com *.gjf *.in *.xyz *.sh *.slurm)",
    "ORCA / CP2K / GAMESS (*.inp)",
    "Gaussian (*.com *.gjf)",
    "Quantum ESPRESSO / VASP / generic (*.in)",
    "Structures (*.xyz)",
    "Scripts (*.sh *.slurm *.pbs)",
    "All files (*)",
)

INPUT_FILTER = ";;".join(INPUT_FILTERS)

RELAY_TITLE = "Reuse another job's file"

BATCH_TEXT = "Submit each file as its own job"

CHAIN_TEXT = "Run after the job already queued on this host"

CHAIN_ANY_TEXT = "...even if that job fails"

FORCE_TEXT = "Force run: start now, ahead of the queue"

NOTHING_TO_FOLLOW = (
    "Nothing queued on this host yet, so there is nothing to run after: "
    "the ticks above are greyed and this job starts straight away."
)

DOWNLOAD_ALL_TEXT = "Download all output files"

BESIDE_INPUT_TEXT = "...next to the input file"

REMOTE_HINT = "Tick the box above to run in a directory that is already on the host."

RELAY_HINT = "Tick the box above to copy a file in from another job on this host."

_SAVE_TEMPLATE = object()

_DELETE_TEMPLATE = object()

_SET_DEFAULT = object()

_MANAGE_TEMPLATES = object()


def with_reason(text: str, reason: str) -> str:
    """A control's own label, carrying why it is greyed (Qt shows no tooltip
    for a disabled widget)."""
    return f"{text} - {reason}" if reason else text
