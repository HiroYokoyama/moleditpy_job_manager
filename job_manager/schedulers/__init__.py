"""Queue-system backends.

Importing this package registers every scheduler, so
``get_scheduler("slurm")`` works without the caller knowing the module layout.
"""

from __future__ import annotations

from .base import (
    STATE_UNKNOWN,
    Scheduler,
    available_schedulers,
    format_command,
    get_scheduler,
    register,
)
from .pbs import PBS
from .sge import SGE
from .shell import SHELL
from .slurm import SLURM
from .windows import WINDOWS

__all__ = [
    "PBS",
    "SGE",
    "SHELL",
    "SLURM",
    "WINDOWS",
    "STATE_UNKNOWN",
    "Scheduler",
    "available_schedulers",
    "format_command",
    "get_scheduler",
    "register",
]
