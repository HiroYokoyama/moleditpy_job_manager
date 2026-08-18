"""One look for every list of files this plugin shows.

Three dialogs show a job's files -- what to download, what to tail, what to
open -- and each had grown its own version of the same list: one with a filter
box and two without, one with emoji in front of every row, three different
folder styles, three different places for the buttons. They are the same list
of the same files, so they are built from the same pieces here.

Only the presentation is shared. What each dialog does with a selection is its
own business, which is why this module holds no dialog of its own.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
)

from .tree_utils import PATH_ROLE

#: The same words in all three, so the box is recognised rather than read.
FILTER_PLACEHOLDER = "Filter files (e.g. .out, .log)"

#: Columns for a listing that knows how big the files are. A remote listing
#: does not, and shows the name alone rather than a column of dashes.
FULL_HEADERS = ("File", "Size", "Type", "Where")
NAME_HEADERS = ("File",)


def configure_tree(
    tree: QTreeWidget,
    headers: Sequence[str] = NAME_HEADERS,
    multi_select: bool = False,
) -> QTreeWidget:
    """Apply the shared look to a file tree."""
    tree.setColumnCount(len(headers))
    tree.setHeaderLabels(list(headers))
    # A single-column list has nothing to label, so its header is a bar of
    # wasted height rather than information.
    tree.setHeaderHidden(len(headers) <= 1)
    tree.setAlternatingRowColors(True)
    tree.setUniformRowHeights(True)
    tree.setSelectionMode(
        QAbstractItemView.SelectionMode.ExtendedSelection
        if multi_select
        else QAbstractItemView.SelectionMode.SingleSelection
    )
    tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    return tree


def folder_factory(checkable: bool = False, columns: int = 1) -> Callable:
    """A folder row: the name in bold, and nothing else in the row.

    Bold rather than an icon character. A folder emoji is drawn from whichever
    font the platform substitutes, at that font's own size, so it sits crushed
    and off-baseline against the text beside it -- and it says nothing the
    indentation and the bold do not.
    """

    def make(name: str, parts: Sequence[str], parent: Optional[QTreeWidgetItem]) -> QTreeWidgetItem:
        # Unattached on purpose: ensure_folder_item owns insertion, and building
        # it with a parent here would insert the same folder twice.
        item = QTreeWidgetItem([name] + [""] * (columns - 1))
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setData(0, PATH_ROLE, None)
        if checkable:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
        else:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        return item

    return make


def make_filter_row(on_changed: Callable[[str], None]) -> tuple:
    """The filter box every one of these lists has. Returns (row, field)."""
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(QLabel("Filter"))
    field = QLineEdit()
    field.setPlaceholderText(FILTER_PLACEHOLDER)
    field.setClearButtonEnabled(True)
    field.textChanged.connect(on_changed)
    row.addWidget(field, 1)
    return row, field


__all__ = [
    "FILTER_PLACEHOLDER",
    "FULL_HEADERS",
    "NAME_HEADERS",
    "configure_tree",
    "folder_factory",
    "make_filter_row",
]
