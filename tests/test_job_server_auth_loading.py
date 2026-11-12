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


# Signing in again ---------------------------------------------------------


def start(held, parent, field, tmp_path, *, again=False, credentials=("alice", "pw-123")):
    on_done = MagicMock()
    asked = MagicMock(return_value=credentials)
    with (
        patch.object(server_backend, "require_client"),
        patch.object(server_backend, "server_credentials", asked),
        patch("job_manager.tasks.run_async", held),
        patch("job_manager.loading_dialog.open_loading_dialog", return_value=MagicMock()) as opens,
        patch("job_manager.loading_dialog.close_loading_dialog"),
        patch("job_manager.store.default_data_dir", return_value=str(tmp_path)),
    ):
        server_backend.show_enrollment(parent, URL, field, "", on_done, again=again)
    return on_done, asked, opens


def enrolled_field(tmp_path):
    identity = tmp_path / "identity"
    identity.mkdir(exist_ok=True)
    old = identity / "old.key"
    old.write_text("synthetic")
    field = MagicMock()
    field.text.return_value = str(old)
    return field, old


def test_sign_in_again_registers_a_new_device_and_replaces_the_old_one(parent, tmp_path):
    held = Held()
    field, old = enrolled_field(tmp_path)
    on_done, asked, opens = start(held, parent, field, tmp_path, again=True)
    asked.assert_called_once()  # Username and password, even though a credential exists.
    assert "Registering this device" in opens.call_args.args[2]
    work, succeeded, _ = held.calls[0]
    client = MagicMock()
    client.request.return_value = {"server_id": "s", "execution_os": "linux"}
    with (
        patch("job_manager.job_server_client.client.JobServerClient", return_value=client) as made,
        patch.object(server_backend, "_device_id", return_value="old-device"),
    ):
        work()
    new_path = made.call_args.args[1]
    assert new_path != old and new_path.parent == tmp_path / "identity"
    client.enroll.assert_called_once_with("alice", "pw-123")
    client.authenticate.assert_not_called()
    assert ("POST", "/me/devices/old-device/revoke") in [
        c.args[:2] for c in client.request.call_args_list
    ]
    assert old.exists()  # Kept until the new one is recorded.
    with (
        patch("job_manager.store.default_data_dir", return_value=str(tmp_path)),
        patch("PySide6.QtWidgets.QMessageBox.information") as told,
    ):
        succeeded({"server_id": "s", "execution_os": "linux"})
    field.setText.assert_called_once_with(str(new_path))
    assert not old.exists()
    assert "Signed in again" in told.call_args.args[2]
    on_done.assert_called_once()


def test_sign_in_again_cancelled_at_the_password_changes_nothing(parent, tmp_path):
    held = Held()
    field, old = enrolled_field(tmp_path)
    start(held, parent, field, tmp_path, again=True, credentials=None)
    assert held.calls == []
    assert old.exists()
    field.setText.assert_not_called()


def test_a_failed_sign_in_again_keeps_the_old_credential(parent, tmp_path):
    held = Held()
    field, old = enrolled_field(tmp_path)
    start(held, parent, field, tmp_path, again=True)
    with patch("PySide6.QtWidgets.QMessageBox.warning"):
        held.calls[0][2]("Invalid credentials")
    assert old.exists()
    field.setText.assert_not_called()


def test_a_refused_sign_in_offers_to_sign_in_again(parent, tmp_path):
    from job_manager.job_server_client.client import ClientError

    held = Held()
    field, _ = enrolled_field(tmp_path)
    _, asked, opens = start(held, parent, field, tmp_path)
    asked.assert_not_called()  # An existing credential signs in without a password.
    assert "Signing in" in opens.call_args.args[2]
    work, _, failed = held.calls[0]
    client = MagicMock()
    client.authenticate.side_effect = ClientError(401, {"message": "Device revoked"})
    with patch("job_manager.job_server_client.client.JobServerClient", return_value=client):
        with pytest.raises(ClientError):
            work()
    from PySide6.QtWidgets import QMessageBox

    with (
        patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ) as asked_again,
        patch.object(server_backend, "show_enrollment") as again,
    ):
        failed("Device revoked")
    assert "no longer accepts" in asked_again.call_args.args[2]
    assert again.call_args.kwargs["again"] is True


def test_an_unreachable_server_is_not_mistaken_for_a_refused_sign_in(parent, tmp_path):
    held = Held()
    field, _ = enrolled_field(tmp_path)
    start(held, parent, field, tmp_path)
    work, _, failed = held.calls[0]
    client = MagicMock()
    client.authenticate.side_effect = OSError("Connection refused")
    with patch("job_manager.job_server_client.client.JobServerClient", return_value=client):
        with pytest.raises(OSError):
            work()
    from PySide6.QtWidgets import QMessageBox

    with (
        patch.object(QMessageBox, "question") as asked_again,
        patch.object(QMessageBox, "warning") as warned,
    ):
        failed("Connection refused")
    asked_again.assert_not_called()
    warned.assert_called_once()


def test_only_credentials_job_manager_made_are_deleted(tmp_path):
    elsewhere = tmp_path / "elsewhere.key"
    elsewhere.write_text("x")
    with patch("job_manager.store.default_data_dir", return_value=str(tmp_path / "data")):
        server_backend._forget_credential(elsewhere)
    assert elsewhere.exists()
