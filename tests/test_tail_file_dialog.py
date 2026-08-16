"""Unit tests for TailFileDialog (tree-style remote file picker for tailing)."""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication


from job_manager.tail_file_dialog import TailFileDialog


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def test_tail_file_dialog_tree_structure(qapp):
    names = [
        "calc.out",
        "calc.log",
        "scratch/temp.xyz",
        "scratch/deep/sub.dat",
    ]
    dlg = TailFileDialog("myjob", names, default_file="calc.out")

    assert dlg.tree.topLevelItemCount() > 0
    assert dlg.chosen() == "calc.out"
    assert dlg.btn_tail.isEnabled()


def test_tail_file_dialog_filter(qapp):
    names = [
        "calc.out",
        "calc.log",
        "scratch/temp.xyz",
    ]
    dlg = TailFileDialog("myjob", names)

    dlg.txt_filter.setText("xyz")
    # After filter, only scratch/temp.xyz should be in tree
    assert dlg.chosen() == "scratch/temp.xyz"

    dlg.txt_filter.setText("nonexistent")
    assert dlg.chosen() == ""
    assert not dlg.btn_tail.isEnabled()


def test_tail_file_dialog_double_click(qapp):
    names = ["job.log", "output.out"]
    dlg = TailFileDialog("myjob", names)

    top_item = dlg.tree.topLevelItem(0)
    dlg._on_item_double_clicked(top_item, 0)
    assert dlg.chosen() == "job.log"
