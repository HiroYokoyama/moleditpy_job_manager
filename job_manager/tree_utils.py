"""Reusable file tree widget builder, hierarchy manager, and filtering utilities.

Unifies QTreeWidgetItem tree construction from slash-separated relative path lists
across download, tail, and output file selector dialogs.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem

PATH_ROLE = Qt.ItemDataRole.UserRole
IS_REMOTE_ROLE = Qt.ItemDataRole.UserRole + 1


def split_path(path: str) -> List[str]:
    """Split a slash- or backslash-separated path into non-empty components."""
    if not path:
        return []
    normalized = path.replace("\\", "/").strip("/")
    return [p for p in normalized.split("/") if p]


def default_folder_factory(
    name: str,
    parts: Sequence[str],
    parent: Optional[QTreeWidgetItem],
) -> QTreeWidgetItem:
    """Create a standard folder QTreeWidgetItem."""
    item = QTreeWidgetItem(
        [name, "", "Folder", ""] if parent is None or parent.columnCount() > 1 else [name]
    )
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
    font = item.font(0)
    font.setBold(True)
    item.setFont(0, font)
    item.setData(0, PATH_ROLE, None)
    item.setData(0, IS_REMOTE_ROLE, None)
    return item


def ensure_folder_item(
    tree: QTreeWidget,
    folders: Dict[Tuple[str, ...], QTreeWidgetItem],
    parts: Sequence[str],
    folder_factory: Optional[
        Callable[[str, Sequence[str], Optional[QTreeWidgetItem]], QTreeWidgetItem]
    ] = None,
) -> Optional[QTreeWidgetItem]:
    """Get or create the QTreeWidgetItem for a folder path and its ancestors.

    Ensures all parent directories exist in the tree and caches created nodes.
    """
    if not parts:
        return None
    key = tuple(parts)
    if key in folders:
        return folders[key]

    parent = ensure_folder_item(tree, folders, parts[:-1], folder_factory)
    factory = folder_factory or default_folder_factory
    item = factory(parts[-1], parts, parent)

    if parent is None:
        tree.addTopLevelItem(item)
    else:
        parent.addChild(item)
    item.setExpanded(True)
    folders[key] = item
    return item


def collect_tree_leaves(
    tree: QTreeWidget,
    path_role: int = PATH_ROLE,
) -> List[QTreeWidgetItem]:
    """Return all leaf file items in the tree that carry path data."""
    found: List[QTreeWidgetItem] = []

    def walk(item: QTreeWidgetItem) -> None:
        if item.data(0, path_role) is not None:
            found.append(item)
        for row in range(item.childCount()):
            walk(item.child(row))

    for row in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(row))
    return found


def find_first_leaf_item(
    tree: QTreeWidget,
    path_role: int = PATH_ROLE,
) -> Optional[QTreeWidgetItem]:
    """Find the first leaf file item in the tree in depth-first order."""

    def walk(item: QTreeWidgetItem) -> Optional[QTreeWidgetItem]:
        if item.data(0, path_role) is not None:
            return item
        for row in range(item.childCount()):
            res = walk(item.child(row))
            if res is not None:
                return res
        return None

    for row in range(tree.topLevelItemCount()):
        res = walk(tree.topLevelItem(row))
        if res is not None:
            return res
    return None


def set_tree_checked_recursive(
    item: QTreeWidgetItem,
    state: Qt.CheckState,
) -> None:
    """Recursively set the check state on an item and all its descendants."""
    item.setCheckState(0, state)
    for row in range(item.childCount()):
        set_tree_checked_recursive(item.child(row), state)


def default_item_matches(item: QTreeWidgetItem, query: str) -> bool:
    """Return True if any column text in the item contains query (case-insensitive)."""
    q = query.lower()
    for col in range(item.columnCount()):
        text = item.text(col)
        if text and q in text.lower():
            return True
    return False


def filter_tree_items(
    tree: QTreeWidget,
    query: str,
    match_fn: Optional[Callable[[QTreeWidgetItem, str], bool]] = None,
    path_role: int = PATH_ROLE,
) -> int:
    """Filter tree items by search text while preserving folder hierarchy.

    If a child file matches, all its ancestor folders remain visible and expanded.
    Non-matching files and empty folders are hidden.
    When query is empty, all items are unhidden.
    Returns the number of visible leaf items.
    """
    clean_query = (query or "").strip().lower()
    matcher = match_fn or default_item_matches

    def apply_visibility(item: QTreeWidgetItem) -> bool:
        is_leaf = item.data(0, path_role) is not None

        if not clean_query:
            item.setHidden(False)
            for row in range(item.childCount()):
                apply_visibility(item.child(row))
            return True

        if is_leaf and item.childCount() == 0:
            matches = matcher(item, clean_query)
            item.setHidden(not matches)
            return matches

        # For a folder (or node with children): check all children
        has_matching_child = False
        for row in range(item.childCount()):
            child_matches = apply_visibility(item.child(row))
            if child_matches:
                has_matching_child = True

        folder_self_matches = matcher(item, clean_query)
        visible = has_matching_child or folder_self_matches
        item.setHidden(not visible)
        if has_matching_child:
            item.setExpanded(True)
        return visible

    for row in range(tree.topLevelItemCount()):
        apply_visibility(tree.topLevelItem(row))

    visible_leaves = 0
    for leaf in collect_tree_leaves(tree, path_role):
        if not leaf.isHidden():
            visible_leaves += 1
    return visible_leaves


__all__ = [
    "IS_REMOTE_ROLE",
    "PATH_ROLE",
    "collect_tree_leaves",
    "default_folder_factory",
    "default_item_matches",
    "ensure_folder_item",
    "filter_tree_items",
    "find_first_leaf_item",
    "set_tree_checked_recursive",
    "split_path",
]
