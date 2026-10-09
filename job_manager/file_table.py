"""Readable, ordered file paths for submission and recorded job files."""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem


class FilePathTable(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["File", "Folder", "Status"])
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setShowGrid(False)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(0, 190)
        self.setColumnWidth(2, 90)
        self.setMinimumHeight(130)
        self.setAccessibleName("Input file paths")

    def paths(self):
        return [self.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(self.rowCount())]

    def add_path(self, path):
        row = self.rowCount()
        self.insertRow(row)
        for column, text in enumerate(
            (
                os.path.basename(path),
                os.path.dirname(path),
                "Available" if os.path.isfile(path) else "Missing",
            )
        ):
            item = QTableWidgetItem(text)
            item.setToolTip(path)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, path)
            self.setItem(row, column, item)

    def selected_rows(self):
        return sorted(index.row() for index in self.selectionModel().selectedRows())

    def remove_selected(self):
        for row in reversed(self.selected_rows()):
            self.removeRow(row)

    def move_selected(self, offset):
        rows = self.selected_rows()
        if not rows or rows[0] + offset < 0 or rows[-1] + offset >= self.rowCount():
            return False
        self.blockSignals(True)
        # Swap whole rows so paths, tooltips and status travel together.
        for row in rows if offset < 0 else reversed(rows):
            other = row + offset
            for column in range(self.columnCount()):
                current = self.takeItem(row, column)
                neighbour = self.takeItem(other, column)
                self.setItem(row, column, neighbour)
                self.setItem(other, column, current)
        self.clearSelection()
        for row in rows:
            self.selectionModel().select(
                self.model().index(row + offset, 0),
                self.selectionModel().SelectionFlag.Select
                | self.selectionModel().SelectionFlag.Rows,
            )
        self.setCurrentCell(rows[0] + offset, 0, self.selectionModel().SelectionFlag.NoUpdate)
        self.blockSignals(False)
        return True
