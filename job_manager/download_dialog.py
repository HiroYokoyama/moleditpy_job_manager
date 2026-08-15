"""Pick which of a job's files to fetch, and where to put them.

The automatic download follows the fetch patterns and says nothing when they
match nothing -- which is the single most common way a finished job appears to
have produced no results. Pressing Download is a deliberate act, so it shows
what is actually in the job directory, ticks whatever the patterns matched, and
lets the user take the rest.

Sub-directories are a tree, because that is what a job directory is: a scratch
folder with forty files in it should be one line that opens, not forty lines in
the middle of the list. Ticking the folder takes everything under it.

The wrapper's own log is listed like anything else. It is never fetched by a
pattern -- it is this plugin's file, not a result -- but a person who wants it
should be able to take it without editing their patterns to say so.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

#: Where a tree item keeps its path relative to the job directory. Only files
#: carry one; a folder item has None, which is how the two are told apart.
PATH_ROLE = Qt.ItemDataRole.UserRole


class DownloadDialog(QDialog):
    """What is on the host, what matched, and where it should land."""

    def __init__(
        self,
        job_name: str,
        names: Sequence[str],
        matched: Sequence[str],
        folder: str,
        title: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(600, 520)
        #: True while check states are being propagated, so a change made in
        #: response to a change does not start another round.
        self._syncing = False
        layout = QVBoxLayout(self)

        if not names:
            headline = "The job directory is empty."
        elif matched:
            headline = f"{len(matched)} of {len(names)} files match the fetch patterns."
        else:
            # The case this dialog exists for.
            headline = (
                f"Nothing matched the fetch patterns. These {len(names)} files are "
                "in the job directory -- tick what you want."
            )
        self.lbl_headline = QLabel(headline)
        self.lbl_headline.setWordWrap(True)
        layout.addWidget(self.lbl_headline)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        # Drag or shift-click a run of files, ctrl-click to add: the tick boxes
        # are the choice, but nobody wants to click forty of them one at a time.
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(self._toggle)
        self._build_tree(names, set(matched))
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree, 1)

        buttons_row = QHBoxLayout()
        self.btn_tick = QPushButton("Tick")
        self.btn_tick.setToolTip("Tick the selected files, or all of them when none is selected.")
        self.btn_tick.clicked.connect(lambda: self._set_selected(Qt.CheckState.Checked))
        self.btn_untick = QPushButton("Untick")
        self.btn_untick.clicked.connect(lambda: self._set_selected(Qt.CheckState.Unchecked))
        buttons_row.addWidget(self.btn_tick)
        buttons_row.addWidget(self.btn_untick)
        buttons_row.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("color: palette(mid);")
        buttons_row.addWidget(self.lbl_count)
        layout.addLayout(buttons_row)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Into"))
        self.txt_folder = QLineEdit(folder)
        self.txt_folder.setToolTip(
            "Where the files land. A file from a sub-directory keeps that "
            "sub-directory here, so scratch/mol.out arrives in scratch/."
        )
        browse = QPushButton("...")
        browse.setMaximumWidth(32)
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.txt_folder, 1)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.btn_download = box.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_download.setText("Download")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        # After the button exists: the count is what disables it.
        self._update_count()

    # --- the tree -----------------------------------------------------------

    def _build_tree(self, names: Sequence[str], matched: set) -> None:
        """One item per path segment; files carry their full relative path."""
        folders: Dict[str, QTreeWidgetItem] = {}

        def folder_for(path: str) -> Optional[QTreeWidgetItem]:
            if not path:
                return None
            if path in folders:
                return folders[path]
            head, _, tail = path.rpartition("/")
            parent = folder_for(head)
            item = QTreeWidgetItem(parent) if parent is not None else QTreeWidgetItem(self.tree)
            item.setText(0, tail or path)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            folders[path] = item
            return item

        for name in names:
            head, _, leaf = name.rpartition("/")
            parent = folder_for(head)
            item = QTreeWidgetItem(parent) if parent is not None else QTreeWidgetItem(self.tree)
            item.setText(0, leaf)
            item.setData(0, PATH_ROLE, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0, Qt.CheckState.Checked if name in matched else Qt.CheckState.Unchecked
            )
        self.tree.expandAll()

    def _leaves(self) -> List[QTreeWidgetItem]:
        found: List[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            if item.data(0, PATH_ROLE):
                found.append(item)
            for row in range(item.childCount()):
                walk(item.child(row))

        for row in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(row))
        return found

    def _on_item_changed(self, item: QTreeWidgetItem, column: int = 0) -> None:
        """Ticking a folder takes everything under it."""
        if self._syncing:
            return
        self._syncing = True
        try:
            if not item.data(0, PATH_ROLE):
                state = item.checkState(0)
                for row in range(item.childCount()):
                    self._set_recursive(item.child(row), state)
        finally:
            self._syncing = False
        self._update_count()

    def _set_recursive(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        item.setCheckState(0, state)
        for row in range(item.childCount()):
            self._set_recursive(item.child(row), state)

    def _toggle(self, item: QTreeWidgetItem, column: int = 0) -> None:
        item.setCheckState(
            0,
            Qt.CheckState.Unchecked
            if item.checkState(0) == Qt.CheckState.Checked
            else Qt.CheckState.Checked,
        )

    def _set_selected(self, state: Qt.CheckState) -> None:
        """Apply to what is selected, or to everything when nothing is."""
        items = self.tree.selectedItems()
        if not items:
            items = [self.tree.topLevelItem(row) for row in range(self.tree.topLevelItemCount())]
        self._syncing = True
        try:
            for item in items:
                self._set_recursive(item, state)
        finally:
            self._syncing = False
        self._update_count()

    def _update_count(self) -> None:
        chosen = len(self.chosen())
        self.lbl_count.setText(f"{chosen} selected" if chosen else "nothing selected")
        self.btn_download.setEnabled(bool(chosen))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Download into", self.txt_folder.text())
        if path:
            self.txt_folder.setText(path)

    # --- what the caller asks for -------------------------------------------

    def chosen(self) -> List[str]:
        """Paths of the ticked files, relative to the job directory."""
        return [
            item.data(0, PATH_ROLE)
            for item in self._leaves()
            if item.checkState(0) == Qt.CheckState.Checked
        ]

    def folder(self) -> str:
        return self.txt_folder.text().strip()


__all__ = ["DownloadDialog", "PATH_ROLE"]
