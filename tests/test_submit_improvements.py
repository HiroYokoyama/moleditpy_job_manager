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


# --- equal-path host detection ----------------------------------------------


def _mirror_host(store, tmp_path, name, folder):
    """A host whose local mirror is ``folder`` under tmp_path."""
    root = tmp_path / folder
    root.mkdir(parents=True, exist_ok=True)
    host = HostProfile(id=f"h-{folder}", name=name, equal_path=str(root))
    store.add_host(host)
    return host, root


def test_a_file_in_a_hosts_mirror_selects_that_host(service, qapp, tmp_path):
    # The detection used to live only in prefill(), so a file that arrived by
    # "Add files..." or a drop onto the open wizard never moved the host --
    # which is every way a user adds one by hand.
    other = HostProfile(id="h-other", name="Other")
    service.store.add_host(other)
    mirror_host, root = _mirror_host(service.store, tmp_path, "Cluster", "share")
    inp = root / "mol.inp"
    inp.write_text("x", encoding="utf-8")

    dlg = SubmitDialog(service)
    dlg.cmb_host.setCurrentIndex(dlg.cmb_host.findData(other.id))
    dlg.add_files([str(inp)])

    assert dlg.current_host().id == mirror_host.id


def test_a_file_outside_every_mirror_leaves_the_host_alone(service, qapp, tmp_path):
    other = HostProfile(id="h-other", name="Other")
    service.store.add_host(other)
    _mirror_host(service.store, tmp_path, "Cluster", "share")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    inp = outside / "mol.inp"
    inp.write_text("x", encoding="utf-8")

    dlg = SubmitDialog(service)
    dlg.cmb_host.setCurrentIndex(dlg.cmb_host.findData(other.id))
    dlg.add_files([str(inp)])

    assert dlg.current_host().id == other.id


def test_a_host_the_user_picked_is_never_overridden(service, qapp, tmp_path):
    # Detection is a better guess than "whichever was used last"; it is not a
    # better answer than the one the user just gave.
    other = HostProfile(id="h-other", name="Other")
    service.store.add_host(other)
    _mirror_host(service.store, tmp_path, "Cluster", "share")
    inp = tmp_path / "share" / "mol.inp"
    inp.write_text("x", encoding="utf-8")

    dlg = SubmitDialog(service)
    dlg.cmb_host.activated.emit(dlg.cmb_host.findData(other.id))
    dlg.cmb_host.setCurrentIndex(dlg.cmb_host.findData(other.id))
    dlg.add_files([str(inp)])

    assert dlg.current_host().id == other.id


def test_the_most_specific_mirror_wins(service, qapp, tmp_path):
    # Both hosts are right about a file under the inner one; the inner is the
    # useful answer. Started on the outer deliberately, or the combo's own
    # default ordering would decide this instead of the detection.
    outer, _outer_root = _mirror_host(service.store, tmp_path, "Outer", "mnt")
    inner, inner_root = _mirror_host(service.store, tmp_path, "Inner", "mnt/hpc")
    inp = inner_root / "mol.inp"
    inp.write_text("x", encoding="utf-8")

    dlg = SubmitDialog(service)
    dlg.cmb_host.setCurrentIndex(dlg.cmb_host.findData(outer.id))
    assert dlg.current_host().id == outer.id
    dlg.add_files([str(inp)])

    assert dlg.current_host().id == inner.id


# --- refusing a job the machine cannot hold ---------------------------------


def _runner_host(cores=8, memory_mb=16384, detect=False):
    from job_manager.models import MODE_RUNNER, SCHEDULER_SHELL

    return HostProfile(
        id="h-ws",
        name="workstation",
        scheduler=SCHEDULER_SHELL,
        concurrency_mode=MODE_RUNNER,
        runner_cores=cores,
        runner_memory_mb=memory_mb,
        runner_detect=detect,
    )


def test_more_cores_than_the_machine_has_is_refused():
    from job_manager.models import SubmitPreset

    message = SubmitDialog._resource_overrun(
        _runner_host(), SubmitPreset(cpus_per_task=16, command_template="x")
    )
    assert "16 cores" in message and "8" in message


def test_more_memory_than_the_machine_has_is_refused():
    from job_manager.models import SubmitPreset

    message = SubmitDialog._resource_overrun(
        _runner_host(), SubmitPreset(cpus_per_task=1, memory="64G", command_template="x")
    )
    assert "64G" in message and "16G" in message


def test_exactly_the_whole_machine_is_allowed():
    from job_manager.models import SubmitPreset

    preset = SubmitPreset(cpus_per_task=8, memory="16G", command_template="x")
    assert SubmitDialog._resource_overrun(_runner_host(), preset) == ""


def test_a_real_cluster_is_never_refused():
    # The queue enforces its own limits and this plugin has no idea how big the
    # nodes are; runner_cores on such a host means nothing.
    from job_manager.models import SubmitPreset

    host = HostProfile(id="h-hpc", name="hpc", scheduler="slurm", runner_cores=8)
    preset = SubmitPreset(cpus_per_task=999, memory="999G", command_template="x")
    assert SubmitDialog._resource_overrun(host, preset) == ""


def test_a_host_that_detects_its_own_resources_is_never_refused():
    # Those numbers are only learned on the machine itself, so there is nothing
    # here to check the request against.
    from job_manager.models import SubmitPreset

    host = _runner_host(cores=0, memory_mb=0, detect=True)
    preset = SubmitPreset(cpus_per_task=999, memory="999G", command_template="x")
    assert SubmitDialog._resource_overrun(host, preset) == ""


# --- a greyed control has to say why ----------------------------------------


def _two_inputs(tmp_path):
    paths = []
    for name in ("a.inp", "b.inp"):
        p = tmp_path / name
        p.write_text("x", encoding="utf-8")
        paths.append(str(p))
    return paths


def test_the_relay_box_says_why_it_is_greyed_on_opening(service, qapp):
    # Qt shows no tooltip for a disabled widget, so a reason kept only there is
    # unreachable exactly when it is wanted -- and this box is greyed from the
    # moment the wizard opens.
    from job_manager.submit_dialog import RELAY_TITLE

    service.store.add_host(HostProfile(id="h1", name="Host1"))
    dlg = SubmitDialog(service)

    assert not dlg.box_relay.isEnabled()
    assert dlg.box_relay.title() != RELAY_TITLE
    assert "input file" in dlg.box_relay.title()


def test_the_relay_box_title_goes_back_to_plain_when_usable(service, qapp, tmp_path):
    from job_manager.submit_dialog import RELAY_TITLE

    service.store.add_host(HostProfile(id="h1", name="Host1"))
    dlg = SubmitDialog(service)
    dlg.add_files(_two_inputs(tmp_path)[:1])

    assert dlg.box_relay.isEnabled()
    assert dlg.box_relay.title() == RELAY_TITLE


def test_each_box_names_the_other_as_the_reason(service, qapp, tmp_path):
    from job_manager.submit_dialog import BATCH_TEXT

    service.store.add_host(HostProfile(id="h1", name="Host1"))
    dlg = SubmitDialog(service)
    dlg.add_files(_two_inputs(tmp_path))

    dlg.chk_batch.setChecked(True)
    assert not dlg.box_relay.isEnabled()
    assert "one job per file" in dlg.box_relay.title()

    dlg.chk_batch.setChecked(False)
    dlg.box_relay.setChecked(True)
    assert not dlg.chk_batch.isEnabled()
    assert "reusing another job's file" in dlg.chk_batch.text()

    dlg.box_relay.setChecked(False)
    assert dlg.chk_batch.isEnabled()
    assert dlg.chk_batch.text() == BATCH_TEXT


def test_work_already_on_the_host_explains_both(service, qapp, tmp_path):
    service.store.add_host(HostProfile(id="h1", name="Host1"))
    dlg = SubmitDialog(service)
    dlg.add_files(_two_inputs(tmp_path))

    dlg.box_remote.setChecked(True)

    assert "already on the host" in dlg.box_relay.title()
    assert "already on the host" in dlg.chk_batch.text()
