"""GUI state regressions found while reading all production source."""

from unittest.mock import Mock
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from job_manager.models import HostProfile, Job, SubmitPreset, STATE_RUNNING
from job_manager.store import JobStore
from job_manager.service import JobService
from job_manager.submit_dialog import SubmitDialog
from job_manager.details_dialog import JobDetailsDialog
from job_manager.result_view import clear_document


@pytest.fixture
def service(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = JobStore(str(tmp_path))
    service = JobService(store)
    yield service
    service.shutdown()
    app.processEvents()


def test_failed_clear_does_not_authorize_opening_result():
    window = Mock()
    window.edit_actions_manager.clear_all.side_effect = RuntimeError("cannot clear")
    assert clear_document(window) is False


def test_batch_resources_follow_each_input(service, tmp_path):
    host = service.store.add_host(HostProfile())
    dialog = SubmitDialog(service)
    first, second = tmp_path / "first.inp", tmp_path / "second.inp"
    first.write_text("%pal nprocs 2 end\n%maxcore 1000")
    second.write_text("%pal nprocs 8 end\n%maxcore 2000")
    dialog.chk_scan_resources.setChecked(True)
    dialog._apply_scanned_resources(str(first))
    preset = SubmitPreset(cpus_per_task=2, memory="2000M")
    service.submit = Mock()
    dialog._submit_batch(host, preset, [str(first), str(second)])
    results = [call.args[1] for call in service.submit.call_args_list]
    assert [(p.cpus_per_task, p.memory) for p in results] == [(2, "2000M"), (8, "16000M")]
    assert preset.cpus_per_task == 2
    dialog.close()


def test_switch_to_unrecognised_input_clears_previous_scan(service, tmp_path):
    dialog = SubmitDialog(service)
    first, second = tmp_path / "first.inp", tmp_path / "second.inp"
    first.write_text("%pal nprocs 8 end\n%maxcore 1000")
    second.write_text("unknown")
    dialog.chk_scan_resources.setChecked(True)
    dialog._apply_scanned_resources(str(first))
    assert dialog.spin_cpus.value() == 8
    dialog._apply_scanned_resources(str(second))
    assert dialog.spin_cpus.value() == 1
    assert dialog.txt_memory.text() == ""
    dialog.close()


def test_submission_worker_uses_copy_and_stale_callback_cannot_change_new_list(
    service, monkeypatch
):
    import job_manager.submission_workflow as workflow

    callbacks = {}

    def queue(pool, work, **kwargs):
        callbacks.update(kwargs)
        callbacks["work"] = work

    monkeypatch.setattr(workflow, "run_async", queue)
    host = HostProfile(scheduler="slurm")
    service.transport_for = Mock(return_value=Mock())

    def submit(transport, host, preset, job, files, **kwargs):
        job.remote_job_id = "123"
        job.state = STATE_RUNNING
        return job

    monkeypatch.setattr(workflow, "submit_job", submit)
    original = service.submit(host, SubmitPreset(), "example", [])
    result = callbacks["work"]()
    assert original.remote_job_id == ""
    assert result is not original
    replacement = Job.from_dict(original.to_dict())
    service.store.jobs[original.id] = replacement
    callbacks["on_success"](result)
    assert replacement.remote_job_id == ""


def test_readonly_and_replaced_details_cannot_write(service):
    job = service.store.add_job(Job(name="original"))
    archived = JobDetailsDialog(service, job, "record", "Details", read_only=True)
    archived.txt_name.setText("changed")
    archived._save()
    assert job.name == "original"
    current = JobDetailsDialog(service, job, "record", "Details")
    service.store.jobs[job.id] = Job.from_dict(job.to_dict())
    current.txt_name.setText("changed")
    current._save()
    assert job.name == "original"
    archived.close()
    current.close()


def test_poll_completion_ignores_reused_id(service):
    original = service.store.add_job(Job(state=STATE_RUNNING))
    poller = service.poller
    poller._poll_sources["host"] = (service.store.jobs_path, {original.id: original})
    replacement = Job.from_dict(original.to_dict())
    service.store.jobs[original.id] = replacement
    poller._on_poll_finished("host", {original.id: "DONE"}, {original.id: 0})
    assert replacement.state == STATE_RUNNING
    assert replacement.rc is None


def test_resource_probe_cannot_fill_another_host_form(monkeypatch):
    from types import SimpleNamespace
    from job_manager import hosts_dialog

    first, second = HostProfile(), HostProfile()
    captured = {}

    def enqueue(pool, work, **callbacks):
        captured.update(callbacks)

    monkeypatch.setattr(hosts_dialog, "run_async", enqueue)
    monkeypatch.setattr(hosts_dialog, "ensure_password", lambda *args: True)
    owner = SimpleNamespace(
        _persist_current=lambda: first,
        _current=first,
        service=SimpleNamespace(pool=object()),
        btn_detect=Mock(),
        lbl_queue=Mock(),
        spin_runner_cores=Mock(),
        spin_runner_memory=Mock(),
        chk_detect_resources=Mock(),
    )
    hosts_dialog.HostsDialog._detect_resources(owner)
    owner._current = second
    captured["on_success"]((32, 128 * 1024, 64))
    owner.spin_runner_cores.setValue.assert_not_called()
    owner.spin_runner_memory.setValue.assert_not_called()


def test_batch_overrun_refuses_every_file(service, tmp_path):
    from job_manager.models import MODE_RUNNER

    host = service.store.add_host(
        HostProfile(concurrency_mode=MODE_RUNNER, runner_detect=False, runner_cores=4)
    )
    dialog = SubmitDialog(service)
    first, second = tmp_path / "first.inp", tmp_path / "second.inp"
    first.write_text("%pal nprocs 2 end")
    second.write_text("%pal nprocs 8 end")
    dialog.chk_scan_resources.setChecked(True)
    service.submit = Mock()
    with pytest.raises(ValueError, match="second.inp"):
        dialog._submit_batch(host, SubmitPreset(), [str(first), str(second)])
    service.submit.assert_not_called()
    dialog.close()


def test_emoji_preserves_qt_cursor_and_match_count(service):
    from job_manager.text_dialog import TextDialog
    from PySide6.QtGui import QTextCursor

    text = "\U0001f600 hit hit"
    dialog = TextDialog("Example", text, follow=False)
    cursor = dialog.view.textCursor()
    cursor.setPosition(7)
    cursor.setPosition(10, QTextCursor.MoveMode.KeepAnchor)
    dialog.view.setTextCursor(cursor)
    assert dialog._match_position("hit") == "2 of 2"
    dialog.set_text(text)
    assert dialog.view.textCursor().selectedText() == "hit"
    dialog.close()
