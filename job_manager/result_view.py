"""Bounded text viewing and safe routing of downloaded results to the host."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from PyQt6.QtWidgets import QWidget

from . import PLUGIN_VERSION

# OpenBabel accepts .txt as molecules; plain text must use our own viewer.
#: Opened in this plugin's own text window, never handed to MoleditPy.
#: OpenBabel lists "txt" as an input format (one empty molecule per line), so
#: with the OpenBabel plugin installed MoleditPy read an output text file as
#: thousands of molecules on the GUI thread and stopped responding -- after
#: first clearing the user's document to make room for a structure that was
#: never coming.
#: The most of a text file shown at once; the end is kept, since that is where
#: an output file says how it finished.
TEXT_EXTENSIONS = (".txt",)
TEXT_VIEW_LIMIT = 5 * 1024 * 1024


def is_text_file(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in TEXT_EXTENSIONS


def show_text_window(path: str, parent: Optional[QWidget] = None):
    """Open ``path`` read-only in a text window, and return the window."""
    from .text_dialog import TextDialog

    def reload() -> None:
        try:
            dialog.set_text(read_text_for_view(path))
        except OSError as exc:
            dialog.set_text(f"Could not read {path}: {exc}")

    # A finished result is read from the top; only a tail starts at the end.
    dialog = TextDialog(
        f"Job Manager {PLUGIN_VERSION} - {os.path.basename(path)}",
        "",
        parent,
        on_refresh=reload,
        follow=False,
        auto_refresh=False,
    )
    dialog.set_text(read_text_for_view(path))
    dialog.present()
    return dialog


def read_text_for_view(path: str, limit: int = TEXT_VIEW_LIMIT) -> str:
    """The file as text, or its last ``limit`` bytes with a line saying so."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        data = handle.read()
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if size > limit:
        # Cut at a line boundary, so the first line shown is a whole one.
        text = text.split("\n", 1)[-1]
        text = f"[Showing the last {limit // (1024 * 1024)} MB of {size:,} bytes]\n\n" + text
    return text


def pick_primary_result(paths: List[str], log_file: str = "") -> str:
    """The file to hand to the application: ranked by what an analyzer plugin
    is most likely to claim, never this plugin's own wrapper log, falling
    back to the first path."""
    from .runner import primary_output

    return primary_output(paths, log_file) or (paths or [""])[0]


def clear_document(main_window) -> bool:
    """Empty the editor so a result opens onto a clean canvas.

    Used to depend on the file's extension: built-in .xyz/.mol loaders
    cleared with the unsaved-changes check skipped (silent data loss), while
    an analyzer plugin (.out, .log) cleared nothing (two molecules on screen
    at once). Cleared here for every route, *with* the check.

    Returns True when the document is clear, including on a host too old to
    have this manager.
    """
    manager = getattr(main_window, "edit_actions_manager", None)
    clear = getattr(manager, "clear_all", None)
    if not callable(clear):
        return True
    try:
        return clear() is not False
    except Exception:
        logging.debug("Job Manager: the document was not cleared", exc_info=True)
        return True


def open_in_host(path: str) -> bool:
    """Route a downloaded file through the application's own file openers.

    Reuses ``MainWindow.init_manager.load_command_line_file``, which walks
    registered plugin openers by priority before the built-in loaders, so no
    analyzer plugin needs to be hard-coded here. Clears the document first --
    see :func:`clear_document`.
    """
    from . import get_context

    context = get_context()
    if context is None or not path or not os.path.exists(path):
        return False
    try:
        main_window = context.get_main_window()
    except Exception:
        logging.debug("Job Manager: no main window available", exc_info=True)
        return False

    if not clear_document(main_window):
        return False

    init_manager = getattr(main_window, "init_manager", None)
    loader = getattr(init_manager, "load_command_line_file", None)
    if callable(loader):
        try:
            loader(path)
            return True
        except Exception:
            logging.warning("Job Manager: host could not open %s", path, exc_info=True)
            return False

    # Older hosts: dispatch to the highest-priority plugin opener directly.
    plugin_manager = getattr(main_window, "plugin_manager", None)
    openers = getattr(plugin_manager, "file_openers", {}) or {}
    extension = os.path.splitext(path)[1].lower()
    for opener in openers.get(extension, []):
        callback = opener.get("callback") if isinstance(opener, dict) else None
        if not callable(callback):
            continue
        try:
            callback(path)
            return True
        except Exception:
            logging.warning("Job Manager: opener failed for %s", path, exc_info=True)
    return False
