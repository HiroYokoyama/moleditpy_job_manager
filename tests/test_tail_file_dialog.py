"""Unit tests for TailFileDialog (tree-style remote file picker for tailing)."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

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


# --- the wrapper's own log is listed, but never offered first ---------------


def test_the_wrapper_log_is_not_preselected(qapp):
    # This window is for what the calculation writes; the wrapper's log is what
    # the Tail Log button already opens. It used to be passed as the default,
    # so the one file this dialog is not for was the one offered.
    dlg = TailFileDialog("myjob", ["job.log", "mol.out"], log_file="job.log")

    assert dlg.chosen() == "mol.out"


def test_the_wrapper_log_is_still_in_the_list(qapp):
    # Not offered first is not the same as removed: it is a real file on the
    # host and asking for it deliberately is allowed.
    dlg = TailFileDialog("myjob", ["job.log", "mol.out"], log_file="job.log")

    listed = {dlg.tree.topLevelItem(row).text(0) for row in range(dlg.tree.topLevelItemCount())}
    assert "job.log" in listed


def test_an_unranked_output_still_beats_the_log(qapp):
    # .gbw is not in RESULT_PRIORITY, so primary_output offers nothing -- and
    # the fallback used to take whatever was listed first, which is the log.
    dlg = TailFileDialog("myjob", ["job.log", "mol.gbw"], log_file="job.log")

    assert dlg.chosen() == "mol.gbw"


def test_the_log_is_selected_when_it_is_the_only_file(qapp):
    # A job whose directory holds nothing else yet: there is nothing to prefer.
    dlg = TailFileDialog("myjob", ["job.log"], log_file="job.log")

    assert dlg.chosen() == "job.log"


def test_the_plugins_other_files_are_not_offered_either(qapp):
    # is_plugin_file covers the sentinel and the runner's own scripts too.
    dlg = TailFileDialog(
        "myjob", [".moleditpy_rc", "moleditpy_run.sh", "mol.out"], log_file="job.log"
    )

    assert dlg.chosen() == "mol.out"


def test_an_explicit_default_still_wins(qapp):
    dlg = TailFileDialog(
        "myjob", ["job.log", "mol.out", "mol.xyz"], default_file="mol.xyz", log_file="job.log"
    )

    assert dlg.chosen() == "mol.xyz"
