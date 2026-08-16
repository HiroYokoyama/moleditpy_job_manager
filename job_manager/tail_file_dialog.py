"""Pick a specific remote file to tail in a clean tree hierarchy.

Builds a folder and file tree from the remote listing so the user can easily
navigate sub-directories (like scratch/) and pick any output or intermediate file
to live-tail.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


from .theme import apply_theme
from .tree_utils import (
    PATH_ROLE,
    ensure_folder_item,
    find_first_leaf_item,
    split_path,
)


class TailFileDialog(QDialog):
    """Tree-style remote file picker for tailing a calculation's output files."""

    def __init__(
        self,
        job_name: str,
        names: Sequence[str],
        default_file: str = "",
        title: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or f"Tail Specific File - {job_name}")
        apply_theme(self)
        self.resize(560, 480)

        self._all_names = list(names)
        self._selected_path: str = ""

        layout = QVBoxLayout(self)

        self.lbl_headline = QLabel(
            f"Select a file from <b>{job_name}</b> to live-tail:"
            if names
            else "No remote files found in the job directory."
        )
        self.lbl_headline.setWordWrap(True)
        layout.addWidget(self.lbl_headline)

        # Quick filter bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("Filter files (e.g. .out, .log, *.xyz)...")
        self.txt_filter.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.txt_filter, 1)
        layout.addLayout(filter_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.tree, 1)

        self._build_tree(self._all_names, default_file)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.btn_tail = box.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_tail.setText("Tail File")
        self.btn_tail.setEnabled(bool(self.chosen()))
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _build_tree(self, names: Sequence[str], default_file: str = "") -> None:
        """One item per path segment; files carry their full relative path."""
        self.tree.clear()
        folders: Dict[tuple, QTreeWidgetItem] = {}
        default_item: Optional[QTreeWidgetItem] = None

        def styled_folder(
            name: str, parts: Sequence[str], parent: Optional[QTreeWidgetItem]
        ) -> QTreeWidgetItem:
            item = QTreeWidgetItem(parent) if parent is not None else QTreeWidgetItem(self.tree)
            item.setText(0, f"📁 {name}")
            item.setData(0, PATH_ROLE, None)
            return item

        for name in names:
            if not name:
                continue
            parts = split_path(name)
            if not parts:
                continue
            parent = ensure_folder_item(
                self.tree, folders, parts[:-1], folder_factory=styled_folder
            )
            leaf = parts[-1]
            item = QTreeWidgetItem(parent) if parent is not None else QTreeWidgetItem(self.tree)
            item.setText(0, f"📄 {leaf}")
            item.setData(0, PATH_ROLE, name)
            if default_file and (name == default_file or leaf == default_file):
                default_item = item

        self._selected_path = ""
        self.tree.expandAll()
        if default_item is not None:
            self.tree.setCurrentItem(default_item)
            self._selected_path = default_item.data(0, PATH_ROLE) or ""
        elif names:
            first = find_first_leaf_item(self.tree)
            if first is not None:
                self.tree.setCurrentItem(first)
                self._selected_path = first.data(0, PATH_ROLE) or ""

    def _apply_filter(self, text: str) -> None:
        filter_text = text.strip().lower()
        if not filter_text:
            filtered = self._all_names
        else:
            filtered = [n for n in self._all_names if filter_text in n.lower()]
        self._build_tree(filtered)
        if hasattr(self, "btn_tail"):
            self.btn_tail.setEnabled(bool(self.chosen()))

    def _on_selection_changed(self) -> None:
        chosen = self.chosen()
        if hasattr(self, "btn_tail"):
            self.btn_tail.setEnabled(bool(chosen))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        path = item.data(0, PATH_ROLE)
        if path:
            self._selected_path = str(path)
            self.accept()

    def chosen(self) -> str:
        """Return the relative path of the selected file, or "" if a folder/nothing is selected."""
        current = self.tree.currentItem()
        if current is not None:
            path = current.data(0, PATH_ROLE)
            if path:
                return str(path)
        return ""
