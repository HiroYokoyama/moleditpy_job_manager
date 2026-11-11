"""Signing in to a Job Server shows that it is waiting, and Cancel stops the waiting only."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from job_manager import server_backend  # noqa: E402

URL = "https://192.168.1.2:8767"


@pytest.fixture
def parent():
    QApplication.instance() or QApplication([])
    widget = QWidget()
    yield widget
    widget.deleteLater()


class Held:
    """run_async that keeps the callbacks, so the test decides when work ends."""

    def __init__(self):
        self.calls = []

    def __call__(self, pool, work, on_success=None, on_error=None, **kwargs):
        self.calls.append((work, on_success, on_error))


def opened(held, parent, *, enrolled=False, tmp_path=None):
    field = MagicMock()
    field.text.return_value = str(tmp_path / "device.key") if enrolled else ""
    if enrolled:
        (tmp_path / "device.key").write_text("synthetic")
    on_done = MagicMock()
    loading = MagicMock()
    with (
        patch.object(server_backend, "require_client"),
        patch.object(server_backend, "server_credentials", return_value=("alice", "pw-123")),
        patch("job_manager.tasks.run_async", held),
        patch("job_manager.loading_dialog.open_loading_dialog", return_value=loading) as opens,
        patch("job_manager.loading_dialog.close_loading_dialog") as closes,
        patch("job_manager.store.default_data_dir", return_value=str(tmp_path)),
    ):
        server_backend.show_enrollment(parent, URL, field, "", on_done)
    return field, on_done, opens, closes


def test_registration_shows_loading_until_the_server_answers(parent, tmp_path):
    held = Held()
    field, on_done, opens, closes = opened(held, parent, tmp_path=tmp_path)
    assert "Registering this device" in opens.call_args.args[2]
    closes.assert_not_called()
    with patch("PySide6.QtWidgets.QMessageBox.information") as told:
        held.calls[0][1]({"server_id": "s", "execution_os": "linux"})
    closes.assert_called_once()
    told.assert_called_once()
    on_done.assert_called_once()
    assert field.setText.called


def test_cancel_still_records_a_device_the_server_registered(parent, tmp_path):
    held = Held()
    field, on_done, opens, closes = opened(held, parent, tmp_path=tmp_path)
    opens.call_args.args[3]()  # Cancel
    closes.assert_called_once()
    with patch("PySide6.QtWidgets.QMessageBox.information") as told:
        held.calls[0][1]({"server_id": "s", "execution_os": "linux"})
    closes.assert_called_once()  # not closed twice
    # No message after the user stopped waiting, but the credential is kept,
    # so trying again does not register a second device.
    told.assert_not_called()
    on_done.assert_called_once()
    assert field.setText.called


def test_cancel_silences_a_later_failure(parent, tmp_path):
    held = Held()
    _, on_done, opens, _ = opened(held, parent, tmp_path=tmp_path)
    opens.call_args.args[3]()
    with patch("PySide6.QtWidgets.QMessageBox.warning") as warned:
        held.calls[0][2]("server unreachable")
    warned.assert_not_called()
    on_done.assert_not_called()


def test_a_failure_closes_the_window_and_says_why(parent, tmp_path):
    held = Held()
    _, _, _, closes = opened(held, parent, tmp_path=tmp_path)
    with patch("PySide6.QtWidgets.QMessageBox.warning") as warned:
        held.calls[0][2]("server unreachable")
    closes.assert_called_once()
    assert "server unreachable" in warned.call_args.args[2]


def test_an_enrolled_device_says_it_is_signing_in(parent, tmp_path):
    _, _, opens, _ = opened(Held(), parent, enrolled=True, tmp_path=tmp_path)
    assert "Signing in" in opens.call_args.args[2]
