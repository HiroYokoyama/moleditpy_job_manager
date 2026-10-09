"""Shared command-substitution filename validation without transports or Qt."""

from __future__ import annotations

import os
from typing import Sequence

#: Characters that would make a file name act as *syntax* in the command it is
#: substituted into. ``{input}`` goes onto the command line as it stands -- it
#: has to, since a template is free to quote it itself -- so a file called
#: ``mol$(id).inp`` would run `id` on the host.
#:
#: A space is deliberately not on this list. A template that writes
#: ``"{input}"`` handles a name with a space in it perfectly well, and refusing
#: those would turn an ordinary file name into a submission error. Nor are the
#: glob characters: they can only ever name another file in the same directory,
#: which is not the same kind of thing at all.
UNSAFE_IN_COMMAND = "\"'`$;&|<>()\\\n\r"

DUPLICATE_UPLOAD_MESSAGE = (
    "Two input files have the same filename. Uploading them together would overwrite one.\n\n"
    "Rename one file, or submit each file as its own job."
)


def check_upload_names(paths: Sequence[str], *, windows: bool = False) -> None:
    """Reject collisions in the flat upload directory, before any files move."""
    names = [os.path.basename(path) for path in paths]
    if windows:
        names = [name.casefold() for name in names]
    if len(names) != len(set(names)):
        raise ValueError(DUPLICATE_UPLOAD_MESSAGE)


def command_unsafe_character(name: str) -> str:
    """The first character of ``name`` a remote shell would act on, or ""."""
    for character in name or "":
        if character in UNSAFE_IN_COMMAND:
            return character
    return ""


def check_input_name(name: str) -> None:
    """Raise if this file name cannot safely be substituted into a command."""
    bad = command_unsafe_character(name)
    if not bad:
        return
    raise ValueError(
        f"The input file is named {name!r}, and a remote shell reads {bad!r} in "
        "it as syntax rather than as part of the name. The name is substituted "
        "into the command line for {input}, so the rest of it would run as a "
        "command of its own. Rename the file and submit again."
    )
