"""Atomic JSON persistence and safe spreadsheet cells."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

#: What a spreadsheet reads as the start of a formula rather than as text.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    """A cell a spreadsheet will show rather than evaluate.

    Excel, LibreOffice and Sheets all treat a cell beginning `=`, `+`, `-` or
    `@` as a formula, whoever wrote it. A job name or a command line is free
    text, an export is a file made to be sent to somebody, and the person who
    opens it is not the person who chose what is in it -- so a leading
    apostrophe goes in front, which is the spelling every one of them reads as
    "this is text".
    """
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_LEAD) else text


def atomic_write_json(path: str, data: Any) -> None:
    """Serialize ``data`` to ``path`` without ever leaving a partial file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".tmp_",
        suffix=".json",
        delete=False,
    )
    tmp_path = handle.name
    try:
        with handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            logging.debug("Job Manager: could not remove temp file %s", tmp_path)
        raise


def read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        logging.warning("Job Manager: could not read %s; using defaults", path)
        return default
