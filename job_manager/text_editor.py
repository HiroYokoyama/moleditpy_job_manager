"""The text viewer for a file: read only until File > Edit Mode is turned on.

A log tail is never edited, so this is separate from :class:`TextDialog`. Viewing
stays the default because most files opened here are results, and Ctrl+S there
means Save As so that a stray key press cannot overwrite one.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

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

from .window_utils import accept_drop_as_copy

_TXT_TOOLTIP = "Plain .txt files are displayed in this text viewer"
_OPEN_TOOLTIP = "Open the source file in MoleditPy and close this viewer"


class _LineNumbers(QWidget):
    """The gutter; :class:`_EditorView` paints it."""

    def __init__(self, view: "_EditorView") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt's spelling
        return QSize(self._view.gutter_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        self._view.paint_gutter(event)


class _EditorView(QPlainTextEdit):
    """A text box with line numbers that leaves dropped files to its window."""

    def __init__(self) -> None:
        super().__init__()
        self._gutter = _LineNumbers(self)
        self.blockCountChanged.connect(lambda _count: self._fit_gutter())
        self.updateRequest.connect(self._scroll_gutter)
        self._fit_gutter()

    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _fit_gutter(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _scroll_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        super().resizeEvent(event)
        area = self.contentsRect()
        self._gutter.setGeometry(area.left(), area.top(), self.gutter_width(), area.height())

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._fit_gutter()
            area = self.contentsRect()
            self._gutter.setGeometry(area.left(), area.top(), self.gutter_width(), area.height())

    def paint_gutter(self, event) -> None:
        painter = QPainter(self._gutter)
        palette = self.palette()
        painter.fillRect(event.rect(), palette.color(QPalette.ColorRole.AlternateBase))
        painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
        painter.setFont(self.font())
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible():
                painter.drawText(
                    0,
                    int(top),
                    self._gutter.width() - 6,
                    height,
                    Qt.AlignmentFlag.AlignRight,
                    str(block.blockNumber() + 1),
                )
            top += self.blockBoundingRect(block).height()
            block = block.next()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if event.key() == Qt.Key.Key_Insert and not self.isReadOnly():
            self.setOverwriteMode(not self.overwriteMode())
            self.cursorPositionChanged.emit()
            return
        super().keyPressEvent(event)

    # A text box takes a dragged file as its path text, and takes it as whatever
    # the drag proposes: Shift would be a move, and Explorer would then delete the
    # file. Ignored here, the drag reaches the window, which opens a copy.
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if event.mimeData().hasUrls():
            event.ignore()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if event.mimeData().hasUrls():
            event.ignore()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if event.mimeData().hasUrls():
            event.ignore()
        else:
            super().dropEvent(event)


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
        self.lbl_position = QLabel()
        self.lbl_position.setStyleSheet("color: palette(mid);")
        self.bottom_row.insertWidget(1, self.lbl_position)
        self.bottom_row.insertSpacing(2, 12)
        self.view.cursorPositionChanged.connect(self._show_position)
        self._base_font = QFont(self.view.font())
        self.layout().insertWidget(2, self._build_replace_bar())
        self.setAcceptDrops(True)
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
        self.act_open = QAction("&Open...", self)
        self.act_open.setShortcut(QKeySequence("Ctrl+O"))
        self.act_open.triggered.connect(lambda _checked=False: self._choose_file())
        file_menu.insertAction(first, self.act_open)
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
        self.act_print = QAction("&Print...", self)
        self.act_print.setShortcut(QKeySequence("Ctrl+P"))
        self.act_print.triggered.connect(lambda _checked=False: self.print_text())
        file_menu.insertAction(self.act_close, self.act_print)
        file_menu.insertSeparator(self.act_close)
        self.act_close.setShortcut(QKeySequence("Ctrl+W"))

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
            ("&Delete\tDel", self._delete_selection),
        ):
            action = QAction(text, self)
            action.triggered.connect(lambda _checked=False, run=slot: run())
            edit_menu.insertAction(before, action)
            self._edit_actions.append(action)
        edit_menu.insertSeparator(before)
        self.act_replace = QAction("R&eplace...", self)
        self.act_replace.setShortcut(QKeySequence("Ctrl+H"))
        self.act_replace.triggered.connect(lambda _checked=False: self.show_replace())
        edit_menu.addAction(self.act_replace)
        self._edit_actions.append(self.act_replace)
        self.act_go_to_line = QAction("&Go to Line...", self)
        self.act_go_to_line.setShortcut(QKeySequence("Ctrl+G"))
        self.act_go_to_line.triggered.connect(lambda _checked=False: self.go_to_line())
        edit_menu.addAction(self.act_go_to_line)

        view_menu = self.menu_view
        view_menu.addSeparator()
        self.act_zoom_in = QAction("Zoom &In", self)
        self.act_zoom_in.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        self.act_zoom_in.triggered.connect(lambda _checked=False: self.view.zoomIn(1))
        view_menu.addAction(self.act_zoom_in)
        self.act_zoom_out = QAction("Zoom &Out", self)
        self.act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        self.act_zoom_out.triggered.connect(lambda _checked=False: self.view.zoomOut(1))
        view_menu.addAction(self.act_zoom_out)
        self.act_zoom_reset = QAction("&Reset Zoom", self)
        self.act_zoom_reset.setShortcut(QKeySequence("Ctrl+0"))
        self.act_zoom_reset.triggered.connect(
            lambda _checked=False: self.view.setFont(QFont(self._base_font))
        )
        view_menu.addAction(self.act_zoom_reset)

    def _make_view(self) -> QPlainTextEdit:
        return _EditorView()

    # --- opening -------------------------------------------------------------

    def _choose_file(self) -> None:
        start = os.path.dirname(self.path) if self.path else ""
        path, _chosen = QFileDialog.getOpenFileName(self, "Open", start, "All files (*)")
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        """In a window of its own, so that this one's changes are not at stake."""
        from .result_view import is_binary_file, show_text_window

        try:
            if is_binary_file(path):
                self._show_status(f"{os.path.basename(path)} appears to be binary.")
                return
            show_text_window(path)
        except OSError as exc:
            self._show_status(f"Could not read {path}: {exc}")

    def _dropped_paths(self, event) -> list:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        return [os.path.normpath(path) for path in paths if os.path.isfile(path)]

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if self._dropped_paths(event):
            accept_drop_as_copy(event)
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        paths = self._dropped_paths(event)
        if not paths:
            event.ignore()
            return
        if accept_drop_as_copy(event):
            # After the drop returns: the drag source is blocked until it does.
            QTimer.singleShot(0, lambda: [self.open_file(path) for path in paths])

    def print_text(self) -> None:
        try:
            from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        except ImportError:
            self._show_status("Printing is not available in this Qt installation.")
            return
        printer = QPrinter()
        if QPrintDialog(printer, self).exec():
            self.view.print_(printer)

    # --- editing -------------------------------------------------------------

    def _delete_selection(self) -> None:
        self.view.textCursor().removeSelectedText()

    def go_to_line(self) -> None:
        document = self.view.document()
        number, chosen = QInputDialog.getInt(
            self,
            "Go to Line",
            f"Line (1 - {document.blockCount()}):",
            self.view.textCursor().blockNumber() + 1,
            1,
            document.blockCount(),
        )
        if chosen:
            self.view.setTextCursor(QTextCursor(document.findBlockByNumber(number - 1)))
            self.view.centerCursor()
            self.view.setFocus()

    def _show_position(self) -> None:
        cursor = self.view.textCursor()
        text = f"Ln {cursor.blockNumber() + 1}, Col {cursor.positionInBlock() + 1}"
        if self.view.overwriteMode() and not self.view.isReadOnly():
            text += " | OVR"
        self.lbl_position.setText(text)

    def _build_replace_bar(self) -> QWidget:
        self.replace_bar = QWidget()
        row = QHBoxLayout(self.replace_bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Replace"))
        self.txt_replace = QLineEdit()
        self.txt_replace.setPlaceholderText("Replacement text")
        self.txt_replace.returnPressed.connect(self.replace_one)
        row.addWidget(self.txt_replace, 1)
        self.btn_replace = QPushButton("Replace")
        self.btn_replace.setAutoDefault(False)
        self.btn_replace.clicked.connect(lambda: self.replace_one())
        row.addWidget(self.btn_replace)
        self.btn_replace_all = QPushButton("Replace All")
        self.btn_replace_all.setAutoDefault(False)
        self.btn_replace_all.clicked.connect(lambda: self.replace_all())
        row.addWidget(self.btn_replace_all)
        self.replace_bar.setVisible(False)
        return self.replace_bar

    def show_replace(self) -> None:
        self.show_find()
        self.replace_bar.setVisible(True)

    def hide_find(self) -> None:
        self.replace_bar.setVisible(False)
        super().hide_find()

    def _selection_matches(self, needle: str) -> bool:
        selected = self.view.textCursor().selectedText()
        if self.chk_case.isChecked():
            return selected == needle
        return selected.lower() == needle.lower()

    def replace_one(self) -> None:
        """Replace the selected match, then select the next one."""
        needle = self.txt_find.text()
        if self.view.isReadOnly() or not needle:
            return
        if self._selection_matches(needle):
            self.view.textCursor().insertText(self.txt_replace.text())
        self.find(False)

    def replace_all(self) -> int:
        """Replace every match as one undo step. Returns how many."""
        needle = self.txt_find.text()
        if self.view.isReadOnly() or not needle:
            return 0
        flags = QTextDocument.FindFlag(0)
        if self.chk_case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        document = self.view.document()
        edit = QTextCursor(document)
        edit.beginEditBlock()
        count = 0
        position = 0
        while True:
            found = document.find(needle, position, flags)
            if found.isNull():
                break
            edit.setPosition(found.selectionStart())
            edit.setPosition(found.selectionEnd(), QTextCursor.MoveMode.KeepAnchor)
            edit.insertText(self.txt_replace.text())
            position = edit.position()
            count += 1
        edit.endEditBlock()
        self.lbl_find.setText(f"Replaced {count}")
        return count

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
        if not on:
            self.replace_bar.setVisible(False)
        self._show_position()

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
