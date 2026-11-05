"""Bounded text viewing and safe routing of downloaded results to the host."""

from __future__ import annotations

import logging
import os
from typing import Callable, List, Optional

from PySide6.QtWidgets import QWidget

from .text_document import TEXT_VIEW_LIMIT as TEXT_VIEW_LIMIT
from .text_document import _text_encoding
from .text_document import read_text_for_view as read_text_for_view

# OpenBabel accepts .txt as molecules; plain text must use our own viewer.
#: Opened in this plugin's own text window, never handed to MoleditPy.
#: OpenBabel lists "txt" as an input format (one empty molecule per line), so
#: with the OpenBabel plugin installed MoleditPy read an output text file as
#: thousands of molecules on the GUI thread and stopped responding -- after
#: first clearing the user's document to make room for a structure that was
#: never coming.
TEXT_EXTENSIONS = (".txt",)


def is_text_file(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in TEXT_EXTENSIONS


def is_binary_file(path: str) -> bool:
    """Inspect a bounded sample instead of guessing from a file extension."""
    with open(path, "rb") as handle:
        sample = handle.read(8192)
    if not sample:
        return False
    text = sample.decode(_text_encoding(sample), errors="replace")
    controls = sum(ord(char) < 32 and char not in "\t\n\r\f\b" for char in text)
    return "\0" in text or controls / len(text) > 0.1


#: A window with no Qt parent lives only as long as something holds it.
_text_windows: list = []


def show_text_window(
    path: Optional[str],
    parent: Optional[QWidget] = None,
    on_opened: Optional[Callable[[], None]] = None,
):
    """Open ``path`` in a text window, or a new empty file when it is None."""
    from .text_editor import TextEditorDialog

    def open_source() -> bool:
        # The window's path, not ``path``: Save As moves it to another file.
        if not open_in_host(dialog.path):
            return False
        if on_opened is not None:
            try:
                on_opened()
            except RuntimeError:
                # The originating browser may already have been destroyed.
                pass
        return True

    from . import get_context

    dialog = TextEditorDialog(
        os.path.abspath(path) if path else None,
        parent,
        open_source if get_context() is not None else None,
    )
    _text_windows.append(dialog)
    dialog.finished.connect(lambda _result: _text_windows.remove(dialog))
    dialog.present()
    return dialog


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
        return False


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

    init_manager = getattr(main_window, "init_manager", None)
    loader = getattr(init_manager, "load_command_line_file", None)
    plugin_manager = getattr(main_window, "plugin_manager", None)
    openers = getattr(plugin_manager, "file_openers", {}) or {}
    extension = os.path.splitext(path)[1].lower()
    registered = [
        opener
        for opener in openers.get(extension, [])
        if isinstance(opener, dict) and callable(opener.get("callback"))
    ]
    builtin = extension in {".mol", ".sdf", ".xyz", ".pmeraw", ".pmeprj"}
    # The host returns None even for unsupported types. Do not clear the
    # document or close the caller merely because that method returned.
    if not registered and not (callable(loader) and builtin):
        return False
    if not clear_document(main_window):
        return False

    if callable(loader):
        try:
            return loader(path) is not False
        except Exception:
            logging.warning("Job Manager: host could not open %s", path, exc_info=True)
            return False

    # Older hosts: dispatch to the highest-priority plugin opener directly.
    for opener in registered:
        callback = opener["callback"]
        try:
            if callback(path) is not False:
                return True
        except Exception:
            logging.warning("Job Manager: opener failed for %s", path, exc_info=True)
    return False
