from html import escape
from unittest.mock import MagicMock
import pytest

pytest.importorskip("PyQt6.QtWidgets")
from job_manager.models import Job
from job_manager.output_file_dialog import OutputFileSelectorDialog
from job_manager.tail_file_dialog import TailFileDialog
from job_manager.host_monitor import HostCard
from .fakes import make_host


def test_job_names_are_escaped_in_output_and_tail_dialogs():
    name = "<img src='audit-marker'>"
    dialogs = [
        OutputFileSelectorDialog(MagicMock(), Job(id="audit", name=name)),
        TailFileDialog(name, ["marker.out"]),
    ]
    try:
        for dialog in dialogs:
            assert escape(name) in dialog.lbl_headline.text()
            assert "<img" not in dialog.lbl_headline.text()
    finally:
        for dialog in dialogs:
            dialog.deleteLater()


def test_host_name_is_escaped_in_monitor():
    name = "<img src='audit-marker'>"
    card = HostCard(make_host(name=name))
    try:
        assert escape(name) in card.lbl_name.text()
        assert "<img" not in card.lbl_name.text()
    finally:
        card.deleteLater()
