"""Submission controls can be reused without constructing a service or wizard."""

from unittest.mock import Mock

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtWidgets import QComboBox, QLineEdit, QWidget  # noqa: E402

from job_manager.command_templates import CommandTemplate  # noqa: E402
from job_manager.host_profile_form import HostProfileForm  # noqa: E402
from job_manager.models import HostProfile  # noqa: E402
from job_manager.store import JobStore  # noqa: E402
from job_manager.submission_resources import SubmissionResources  # noqa: E402
from job_manager.submission_templates import SubmissionTemplates  # noqa: E402


def test_download_controls_persist_preferences_without_a_submit_dialog(tmp_path):
    store = JobStore(str(tmp_path))
    callbacks = {
        name: Mock()
        for name in (
            "_on_force_toggled",
            "_on_scan_resources_toggled",
            "_on_template_chosen",
            "_refresh_preview",
        )
    }
    panel = SubmissionResources(store, callbacks)
    try:
        panel.chk_auto_download.setChecked(False)
        assert not panel.chk_download_all.isEnabled()
        assert not panel.txt_download_root.isEnabled()
        assert JobStore(str(tmp_path)).get_pref("auto_download") is False
        panel.chk_auto_download.setChecked(True)
        assert panel.chk_download_all.isEnabled()
        assert panel.txt_download_root.isEnabled()
        assert JobStore(str(tmp_path)).get_pref("auto_download") is True
    finally:
        sip.delete(panel)


def test_template_switching_preserves_user_edited_fetch_patterns(tmp_path):
    parent = QWidget()
    command = QLineEdit(parent)
    globs = QLineEdit(parent)
    combo = QComboBox(parent)
    templates = SubmissionTemplates(
        JobStore(str(tmp_path)), lambda: [], command, globs, combo, parent
    )
    try:
        templates._apply_template_globs(
            CommandTemplate("first", "mycommand", fetch_globs=("*.out",))
        )
        assert globs.text() == "*.out"
        globs.setText("*.cube, *.xyz")
        templates._apply_template_globs(
            CommandTemplate("next", "mycommand", fetch_globs=("*.log",))
        )
        assert globs.text() == "*.cube, *.xyz"
    finally:
        sip.delete(parent)


def test_collecting_host_fields_does_not_need_a_store_or_network():
    callbacks = {
        name: Mock()
        for name in (
            "_apply_queue_limits",
            "_detect_resources",
            "_on_detect_toggled",
            "_on_pause_toggled",
            "_suggest_local_scheduler",
            "_update_backend_hint",
            "_update_concurrency_row",
        )
    }
    form = HostProfileForm(callbacks)
    try:
        form.txt_name.setText("myhost")
        form.txt_remote_root.setText("/scratch/jobs")
        form.chk_detect_resources.setChecked(True)
        form.spin_runner_cores.setValue(24)
        form.spin_runner_memory.setValue(128)
        host = form._collect(HostProfile())
        assert host.name == "myhost"
        assert host.remote_root == "/scratch/jobs"
        assert host.runner_cores == 0
        assert host.runner_memory_mb == 0
    finally:
        sip.delete(form)
