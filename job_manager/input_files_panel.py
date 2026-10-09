"""Input-path entry, selection and ordering, independent of job submission."""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from .file_table import FilePathTable


class InputFilesPanel(QGroupBox):
    browse_requested = pyqtSignal()
    paths_added = pyqtSignal(object)
    files_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Input files to upload", parent)
        self.setToolTip(
            "Optional. With none, the command runs on its own in a new directory on the host."
        )
        files_layout = QVBoxLayout(self)
        files_note = QLabel("Optional. The first one is passed to the command as {input}.")
        files_note.setWordWrap(True)
        files_note.setStyleSheet("color: palette(mid);")
        files_note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        files_layout.addWidget(files_note)
        self.file_table = FilePathTable(self)
        files_layout.addWidget(self.file_table)
        self.txt_selected_path = QLineEdit()
        self.txt_selected_path.setReadOnly(True)
        self.txt_selected_path.setPlaceholderText("Select a file to read or copy its full path")
        self.txt_selected_path.setAccessibleName("Selected file full path")
        files_layout.addWidget(self.txt_selected_path)
        self.file_table.itemSelectionChanged.connect(self._update_file_selection)
        row = QHBoxLayout()
        add = QPushButton("Add files...")
        add.clicked.connect(self.browse_requested.emit)
        self.btn_remove_file = QPushButton("Remove selected")
        self.btn_remove_file.clicked.connect(self._remove_file)
        self.btn_file_up = QPushButton("Move up")
        self.btn_file_down = QPushButton("Move down")
        self.btn_file_up.clicked.connect(lambda: self._move_files(-1))
        self.btn_file_down.clicked.connect(lambda: self._move_files(1))
        row.addWidget(add)
        row.addWidget(self.btn_remove_file)
        row.addWidget(self.btn_file_up)
        row.addWidget(self.btn_file_down)
        row.addStretch(1)
        files_layout.addLayout(row)
        paste_row = QHBoxLayout()
        self.txt_add_paths = QPlainTextEdit()
        self.txt_add_paths.setPlaceholderText(
            "Paste file paths here, one per line (quotes are optional)"
        )
        self.txt_add_paths.setAccessibleName("File paths to add")
        self.txt_add_paths.setMaximumHeight(72)
        paste_row.addWidget(self.txt_add_paths, 1)
        paste = QPushButton("Add paths")
        paste.clicked.connect(self._add_pasted_paths)
        paste_row.addWidget(paste)
        files_layout.addLayout(paste_row)
        self.lbl_file_message = QLabel("")
        self.lbl_file_message.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_file_message.setWordWrap(True)
        files_layout.addWidget(self.lbl_file_message)
        self._update_file_selection()
        self.files_layout = files_layout

    def selected_files(self):
        return self.file_table.paths()

    def _update_file_selection(self) -> None:
        rows = self.file_table.selected_rows()
        paths = self.selected_files()
        self.txt_selected_path.setText(paths[rows[0]] if rows else "")
        self.txt_selected_path.setCursorPosition(0)
        self.btn_remove_file.setEnabled(bool(rows))
        self.btn_file_up.setEnabled(bool(rows) and rows[0] > 0)
        self.btn_file_down.setEnabled(bool(rows) and rows[-1] < len(paths) - 1)

    def _add_pasted_paths(self) -> None:
        from PyQt6.QtCore import QUrl

        paths = []
        for line in self.txt_add_paths.toPlainText().splitlines():
            path = line.strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
                path = path[1:-1]
            if not path:
                continue
            if path.lower().startswith("file:"):
                url = QUrl(path)
                if not url.isLocalFile():
                    self.lbl_file_message.setText("Use a local file path or file URL.")
                    return
                path = url.toLocalFile()
            path = os.path.abspath(os.path.expanduser(path))
            if not os.path.isfile(path):
                self.lbl_file_message.setText(f"File not found: {path}. No paths were added.")
                return
            paths.append(path)
        if not paths:
            self.lbl_file_message.setText("Enter at least one file path.")
            return
        self.paths_added.emit(paths)
        self.txt_add_paths.clear()

    def _move_files(self, offset: int) -> None:
        if self.file_table.move_selected(offset):
            self._update_file_selection()
            self.files_changed.emit()

    def _remove_file(self) -> None:
        self.file_table.remove_selected()
        self._update_file_selection()
        self.files_changed.emit()
