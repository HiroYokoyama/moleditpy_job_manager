"""Unit tests for tree_utils (shared tree builder, leaf collector, check recursion, filter)."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

from job_manager.tree_utils import (
    PATH_ROLE,
    collect_tree_leaves,
    ensure_folder_item,
    filter_tree_items,
    find_first_leaf_item,
    set_tree_checked_recursive,
    split_path,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def test_split_path():
    assert split_path("a/b/c.out") == ["a", "b", "c.out"]
    assert split_path(r"a\b\c.out") == ["a", "b", "c.out"]
    assert split_path("/a/b/") == ["a", "b"]
    assert split_path("") == []
    assert split_path("single.txt") == ["single.txt"]


def test_ensure_folder_item_hierarchy(qapp):
    tree = QTreeWidget()
    folders = {}

    item1 = ensure_folder_item(tree, folders, ["scratch", "sub"])
    assert item1 is not None
    assert tree.topLevelItemCount() == 1

    top_item = tree.topLevelItem(0)
    assert top_item.text(0) == "scratch"
    assert top_item.childCount() == 1
    assert top_item.child(0) == item1
    assert item1.text(0) == "sub"

    # Reuse cached folder
    item1_again = ensure_folder_item(tree, folders, ["scratch", "sub"])
    assert item1_again is item1
    assert tree.topLevelItemCount() == 1


def test_collect_tree_leaves_and_find_first(qapp):
    tree = QTreeWidget()
    folders = {}

    parent = ensure_folder_item(tree, folders, ["scratch"])
    leaf1 = QTreeWidgetItem(parent, ["file1.txt"])
    leaf1.setData(0, PATH_ROLE, "scratch/file1.txt")

    leaf2 = QTreeWidgetItem(tree, ["root.out"])
    leaf2.setData(0, PATH_ROLE, "root.out")

    leaves = collect_tree_leaves(tree)
    assert len(leaves) == 2
    assert leaves == [leaf1, leaf2]

    first = find_first_leaf_item(tree)
    assert first is leaf1


def test_set_tree_checked_recursive(qapp):
    tree = QTreeWidget()
    folders = {}

    parent = ensure_folder_item(tree, folders, ["folder"])
    parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    parent.setCheckState(0, Qt.CheckState.Unchecked)

    child = QTreeWidgetItem(parent, ["child.txt"])
    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    child.setCheckState(0, Qt.CheckState.Unchecked)

    set_tree_checked_recursive(parent, Qt.CheckState.Checked)
    assert parent.checkState(0) == Qt.CheckState.Checked
    assert child.checkState(0) == Qt.CheckState.Checked


def test_filter_tree_items_keeps_parent_folders_visible(qapp):
    tree = QTreeWidget()
    folders = {}

    parent = ensure_folder_item(tree, folders, ["results", "deep"])
    leaf_match = QTreeWidgetItem(parent, ["opt.xyz"])
    leaf_match.setData(0, PATH_ROLE, "results/deep/opt.xyz")

    leaf_nomatch = QTreeWidgetItem(parent, ["other.log"])
    leaf_nomatch.setData(0, PATH_ROLE, "results/deep/other.log")

    unrelated = ensure_folder_item(tree, folders, ["unrelated"])
    unrelated_leaf = QTreeWidgetItem(unrelated, ["foo.dat"])
    unrelated_leaf.setData(0, PATH_ROLE, "unrelated/foo.dat")

    # Filter for 'xyz'
    visible_count = filter_tree_items(tree, "xyz")
    assert visible_count == 1
    assert not leaf_match.isHidden()
    assert leaf_nomatch.isHidden()
    assert not parent.isHidden()
    assert not tree.topLevelItem(0).isHidden()  # 'results'
    assert tree.topLevelItem(1).isHidden()  # 'unrelated'

    # Clear filter
    filter_tree_items(tree, "")
    assert not leaf_match.isHidden()
    assert not leaf_nomatch.isHidden()
    assert not tree.topLevelItem(0).isHidden()
    assert not tree.topLevelItem(1).isHidden()
