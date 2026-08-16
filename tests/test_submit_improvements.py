"""Tests for submit dialog enhancements: auto-download, download all, resource scan, and command suggestions."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from job_manager.models import HostProfile
from job_manager.service import JobService
from job_manager.store import JobStore
from job_manager.submit_dialog import SubmitDialog


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def temp_store(tmp_path):
    return JobStore(str(tmp_path))


@pytest.fixture
def service(temp_store):
    return JobService(temp_store)


def test_submit_dialog_scan_resources_disables_inputs(service, qapp):
    host = HostProfile(id="h1", name="Host1")
    service.store.add_host(host)

    dlg = SubmitDialog(service)
    dlg.chk_scan_resources.setChecked(True)
    assert not dlg.spin_cpus.isEnabled()
    assert not dlg.txt_memory.isEnabled()

    dlg.chk_scan_resources.setChecked(False)
    assert dlg.spin_cpus.isEnabled()
    assert dlg.txt_memory.isEnabled()


def test_submit_dialog_auto_download_persistence(service, qapp):
    host = HostProfile(id="h1", name="Host1")
    service.store.add_host(host)

    dlg1 = SubmitDialog(service)
    dlg1.chk_auto_download.setChecked(False)
    assert service.store.get_pref("auto_download") is False

    # Next submit dialog should open with False
    dlg2 = SubmitDialog(service)
    assert dlg2.chk_auto_download.isChecked() is False


def test_submit_dialog_download_all_outputs(service, qapp):
    host = HostProfile(id="h1", name="Host1")
    service.store.add_host(host)

    dlg = SubmitDialog(service)
    assert dlg.chk_download_all.isChecked() is True

    dlg.chk_download_all.setChecked(False)
    assert service.store.get_pref("download_all_outputs") is False

    dlg.txt_globs.setText("*.out, *.log")
    preset2 = dlg.collect_preset()
    assert preset2.fetch_globs == ["*.out", "*.log"]



def test_submit_dialog_prefill_evaluates_extensions(service, qapp, tmp_path):
    host = HostProfile(id="h1", name="Host1")
    service.store.add_host(host)

    # Gaussian .gjf file
    gjf = tmp_path / "water.gjf"
    gjf.write_text("# B3LYP/6-31G(d)\n\nwater\n\n0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n\n")

    dlg = SubmitDialog(service)
    dlg.prefill(files=[str(gjf)], name="water")
    assert "g16" in dlg.txt_command.text()
    assert "*.chk" in dlg.txt_globs.text() or "*.log" in dlg.txt_globs.text()

    # ORCA .inp file
    inp = tmp_path / "calc.inp"
    inp.write_text("! B3LYP def2-SVP\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*\n")

    dlg2 = SubmitDialog(service)
    dlg2.prefill(files=[str(inp)], name="calc")
    assert "orca" in dlg2.txt_command.text().lower()
