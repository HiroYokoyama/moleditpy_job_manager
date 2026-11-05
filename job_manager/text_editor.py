"""The text viewer for a file: read only until File > Edit Mode is turned on.

A log tail is never edited, so this is separate from :class:`TextDialog`. Viewing
stays the default because most files opened here are results, and Ctrl+S there
means Save As so that a stray key press cannot overwrite one.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Callable, Optional, Tuple

from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton, QWidget

from . import PLUGIN_VERSION
from .text_dialog import TextDialog
from .text_document import (
    ENCODINGS,
    NEWLINES,
    TextFormat,
    copy_file,
    file_stamp,
    read_document,
    write_bytes,
)

_TXT_TOOLTIP = "Plain .txt files are displayed in this text viewer"
_OPEN_TOOLTIP = "Open the source file in MoleditPy and close this viewer"


class TextEditorDialog(TextDialog):
    """One file, or an unsaved new one when ``path`` is None."""

    def __init__(
        self,
        path: Optional[str],
        parent: Optional[QWidget] = None,
        on_open_in_moleditpy: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__(
            "",
            "",
            parent,
            on_refresh=self._reload,
            follow=False,
            auto_refresh=False,
            on_open_in_moleditpy=on_open_in_moleditpy,
        )
        self.path = path
        self._format = TextFormat()
        self._editable = True
        #: A format choice is a change the undo stack does not know about.
        self._format_changed = False
        self._stamp: Optional[Tuple[int, int]] = None

        self.lbl_format = QLabel()
        self.lbl_format.setStyleSheet("color: palette(mid);")
        self.bottom_row.insertWidget(0, self.lbl_format)
        self.btn_save = QPushButton("Save")
        self.btn_save.setAutoDefault(False)
        self.btn_save.clicked.connect(lambda: self.save())
        self.button_box.addButton(self.btn_save, self.button_box.ButtonRole.ActionRole)
        self._build_edit_menus()
        self.view.document().modificationChanged.connect(lambda _on: self._show_modified())

        if path is None:
            self.act_edit_mode.setChecked(True)
        else:
            self._apply_edit_mode(False)
            self._load()
        self._sync_file_state()

    # --- menus ---------------------------------------------------------------

    def _build_edit_menus(self) -> None:
        file_menu = self.menu_file
        first = file_menu.actions()[1]  # After New.
        self.act_edit_mode = QAction("&Edit Mode", self)
        self.act_edit_mode.setCheckable(True)
        self.act_edit_mode.setShortcut(QKeySequence("Ctrl+E"))
        self.act_edit_mode.toggled.connect(self._on_edit_mode_toggled)
        file_menu.insertAction(first, self.act_edit_mode)
        file_menu.insertSeparator(first)

        self.act_save = QAction("&Save", self)
        self.act_save.triggered.connect(lambda _checked=False: self.save())
        self.act_save.setEnabled(False)
        file_menu.insertAction(self.act_save_as, self.act_save)

        anchor = self.act_reload
        self.menu_reopen = file_menu.addMenu("Reopen with E&ncoding")
        self.menu_save_encoding = file_menu.addMenu("Save En&coding")
        self.menu_save_newline = file_menu.addMenu("Save &Line Endings")
        for menu in (self.menu_save_encoding, self.menu_save_newline, self.menu_reopen):
            file_menu.removeAction(menu.menuAction())
        for menu in (self.menu_reopen, self.menu_save_encoding, self.menu_save_newline):
            file_menu.insertMenu(anchor, menu)
        file_menu.insertSeparator(anchor)

        self._encoding_actions = QActionGroup(self)
        self._newline_actions = QActionGroup(self)
        for label, codec, bom in ENCODINGS:
            choice = (codec, bom)
            action = self.menu_reopen.addAction(label)
            action.triggered.connect(lambda _on=False, c=choice: self._reopen(c))
            action = self.menu_save_encoding.addAction(label)
            action.setCheckable(True)
            action.setData(choice)
            action.triggered.connect(lambda _on=False, c=choice: self._choose_encoding(c))
            self._encoding_actions.addAction(action)
        for label, newline in NEWLINES:
            action = self.menu_save_newline.addAction(label)
            action.setCheckable(True)
            action.setData(newline)
            action.triggered.connect(lambda _on=False, n=newline: self._choose_newline(n))
            self._newline_actions.addAction(action)

        edit_menu = self.menu_edit
        before = edit_menu.actions()[0]
        self._edit_actions = []
        # The shortcuts are the text box's own, so these only name them.
        for text, slot in (
            ("&Undo\tCtrl+Z", self.view.undo),
            ("&Redo\tCtrl+Y", self.view.redo),
            ("Cu&t\tCtrl+X", self.view.cut),
            ("&Paste\tCtrl+V", self.view.paste),
        ):
            action = QAction(text, self)
            action.triggered.connect(lambda _checked=False, run=slot: run())
            edit_menu.insertAction(before, action)
            self._edit_actions.append(action)
        edit_menu.insertSeparator(before)

    def _on_edit_mode_toggled(self, on: bool) -> None:
        if not on and not self._may_discard():
            self.act_edit_mode.blockSignals(True)
            self.act_edit_mode.setChecked(True)
            self.act_edit_mode.blockSignals(False)
            return
        if not on and self.path is not None:
            # Whatever was discarded goes; the view shows the file again.
            self._load()
        self._apply_edit_mode(on)

    def _apply_edit_mode(self, on: bool) -> None:
        self.view.setReadOnly(not on)
        self.act_save.setEnabled(on)
        # Enabled as well: a button box shows all its buttons again when it lays
        # itself out anew, as it does on a style change.
        self.btn_save.setEnabled(on)
        self.btn_save.setVisible(on)
        self.menu_save_encoding.setEnabled(on)
        self.menu_save_newline.setEnabled(on)
        for action in self._edit_actions:
            action.setEnabled(on)
        # Ctrl+S overwrites only where overwriting was asked for.
        self.act_save.setShortcut(QKeySequence("Ctrl+S") if on else QKeySequence())
        self.act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S" if on else "Ctrl+S"))

    # --- the file ------------------------------------------------------------

    def _load(self, encoding: Optional[Tuple[str, bytes]] = None) -> None:
        document = read_document(self.path, encoding=encoding)
        self.set_text(document.text)
        self.view.document().setModified(False)
        self._format = document.format
        self._format_changed = False
        self._editable = document.editable
        self._stamp = file_stamp(self.path)
        self.lbl_open_status.setText(document.note)
        self.lbl_open_status.setVisible(bool(document.note))
        if not document.editable and self.act_edit_mode.isChecked():
            self.act_edit_mode.blockSignals(True)
            self.act_edit_mode.setChecked(False)
            self.act_edit_mode.blockSignals(False)
            self._apply_edit_mode(False)
        self._sync_file_state()

    def _reload(self) -> None:
        if self.path is None or not self._may_discard():
            return
        try:
            self._load()
        except OSError as exc:
            self.set_text(f"Could not read {self.path}: {exc}")
            self.view.document().setModified(False)
            self._editable = False
            self._sync_file_state()

    def _reopen(self, encoding: Tuple[str, bytes]) -> None:
        if self.path is None or not self._may_discard():
            return
        try:
            self._load(encoding)
        except OSError as exc:
            self._show_status(f"Could not read this file: {exc}")
        except UnicodeError:
            self._show_status("This file is not valid in that encoding, so it was left as it was.")

    def _sync_file_state(self) -> None:
        name = os.path.basename(self.path) if self.path else "Untitled"
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - {name}[*]")
        self.lbl_format.setText(self._format.label())
        self.act_edit_mode.setEnabled(self._editable)
        self.act_reload.setEnabled(self.path is not None)
        self.menu_reopen.setEnabled(self.path is not None)
        for action in self._encoding_actions.actions():
            action.setChecked(action.data() == (self._format.encoding, self._format.bom))
        for action in self._newline_actions.actions():
            action.setChecked(action.data() == self._format.newline)
        if self.btn_open_in_moleditpy is not None:
            text_only = (self.path or "").lower().endswith(".txt")
            # OpenBabel's .txt importer treats arbitrary text as molecules and
            # can freeze the editor.
            self.btn_open_in_moleditpy.setEnabled(self.path is not None and not text_only)
            self.btn_open_in_moleditpy.setToolTip(_TXT_TOOLTIP if text_only else _OPEN_TOOLTIP)
        self._show_modified()

    def _show_modified(self) -> None:
        self.setWindowModified(self.is_modified())

    def is_modified(self) -> bool:
        return self.view.document().isModified() or self._format_changed

    def _choose_encoding(self, encoding: Tuple[str, bytes]) -> None:
        codec, bom = encoding
        self._choose_format(dataclasses.replace(self._format, encoding=codec, bom=bom))

    def _choose_newline(self, newline: str) -> None:
        self._choose_format(
            dataclasses.replace(self._format, newline=newline, mixed_newlines=False)
        )

    def _choose_format(self, text_format: TextFormat) -> None:
        if text_format != self._format:
            self._format = text_format
            self._format_changed = True
        self._sync_file_state()

    # --- saving --------------------------------------------------------------

    def _suggested_save_path(self) -> str:
        return self.path or ""

    def _write_to(self, path: str) -> None:
        if self.path and not self.is_modified() and os.path.isfile(self.path):
            # Unedited, so byte for byte -- and whole, even where only the end
            # of the file is on screen.
            copy_file(self.path, path)
        elif self._editable:
            write_bytes(path, self._format.encode(self._view_text()))
        else:
            raise OSError("the source file is no longer there")

    def _saved_to(self, path: str) -> None:
        super()._saved_to(path)
        self.path = os.path.abspath(path)
        self.view.document().setModified(False)
        self._format_changed = False
        self._stamp = file_stamp(self.path)
        self._sync_file_state()

    def save(self) -> bool:
        """Overwrite the file; a new one has nowhere to go until it is named."""
        if self.path is None:
            return self.save_as()
        current = file_stamp(self.path)
        if current is not None and current != self._stamp:
            confirm = QMessageBox.question(
                self,
                "File changed",
                f"{os.path.basename(self.path)} has changed on disk since it was opened.\n\n"
                "Overwrite it with this text?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return False
        return self._save_to(self.path)

    def _may_discard(self) -> bool:
        """Settle unsaved changes. False means the user wants to stay."""
        if not self.is_modified():
            return True
        buttons = QMessageBox.StandardButton
        name = os.path.basename(self.path) if self.path else "Untitled"
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"Save the changes to {name}?",
            buttons.Save | buttons.Discard | buttons.Cancel,
            buttons.Save,
        )
        if answer == buttons.Save:
            return self.save()
        if answer == buttons.Discard:
            self.view.document().setModified(False)
            self._format_changed = False
            self._show_modified()
            return True
        return False

    def _open_in_moleditpy(self) -> None:
        # MoleditPy reads the file, not this text.
        if self._may_discard():
            super()._open_in_moleditpy()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if not self._may_discard():
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        # Esc and the Close button come through here without a closeEvent.
        if self._may_discard():
            super().reject()


__all__ = ["TextEditorDialog"]
