"""High-level backend contract for Job Manager; no remote shell transport."""

import os
import uuid
from pathlib import Path
import importlib.util
import re
from importlib.metadata import PackageNotFoundError, version
from .job_server_client.connection import validate_server_url
from urllib.parse import quote

from .job_server_client.files import validate_path

INSTALL_HINT = "Job Server support needs optional cryptography >=50.0.2. Install with: python -m pip install --upgrade 'cryptography>=50.0.2'"


def client_available():
    try:
        if importlib.util.find_spec("cryptography") is None:
            return False
        release = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version("cryptography"))
        return bool(release and tuple(map(int, release.groups())) >= (50, 0, 2))
    except (PackageNotFoundError, ImportError, ValueError):
        return False


def require_client():
    if not client_available():
        raise ValueError(INSTALL_HINT)


def backend_for(host):
    return JobServerBackend(host)


def server_credentials(parent):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit

    dialog = QDialog(parent)
    dialog.setWindowTitle("Sign in to Job Server")
    layout = QFormLayout(dialog)
    username, password = QLineEdit(dialog), QLineEdit(dialog)
    username.setObjectName("username")
    password.setObjectName("password")
    username.setMaxLength(64)
    password.setMaxLength(1024)
    password.setEchoMode(QLineEdit.EchoMode.Password)
    layout.addRow("Username", username)
    layout.addRow("Password", password)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=dialog
    )
    accept = buttons.button(QDialogButtonBox.StandardButton.Ok)
    accept.setText("Sign in")

    def validate():
        accept.setEnabled(bool(username.text().strip() and password.text()))

    username.textChanged.connect(validate)
    password.textChanged.connect(validate)
    validate()
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    username.setFocus()
    result = None
    if dialog.exec() == QDialog.DialogCode.Accepted:
        result = username.text().strip(), password.text()
    password.clear()
    dialog.deleteLater()
    return result


def fetch_ca_for_confirmation(url):
    """Download the public CA, with the fingerprint to show before anything is trusted."""
    from .job_server_client.trust import certificate_fingerprint, fetch_server_ca

    pem = fetch_server_ca(url)
    return pem, certificate_fingerprint(pem)


def confirm_ca_fingerprint(parent, fingerprint, destination):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Trust local server CA")
    box.setText(
        "Compare this SHA-256 fingerprint with the one shown on the server PC.\n\n"
        f"{fingerprint}\n\n"
        "Trust this CA only if they match. If they differ, something else may be "
        "answering at this address.\n\n" + destination
    )
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    confirmed = box.exec() == QMessageBox.StandardButton.Yes
    box.deleteLater()
    return confirmed


def install_server_ca(url, pem, fingerprint, directory, *, windows_store=None):
    from .job_server_client.client import JobServerClient
    from .job_server_client.files import atomic_write
    from .job_server_client.trust import (
        normalize_fingerprint,
        trust_windows_user_ca,
        validate_ca,
    )

    validate_ca(pem, fingerprint)
    path = Path(directory) / "identity" / "ca" / (normalize_fingerprint(fingerprint) + ".pem")
    atomic_write(path, pem)
    windows_store = os.name == "nt" if windows_store is None else windows_store
    if windows_store:
        try:
            trust_windows_user_ca(pem, fingerprint)
        except OSError as exc:
            raise RuntimeError(
                f"CA saved at {path}, but Windows trust registration failed: {exc}"
            ) from exc
        ca_file = ""
    else:
        ca_file = str(path)
    JobServerClient(url, ca_file=ca_file or None, timeout=5).request(
        "GET", "/health", authenticated=False
    )
    return {"ca_file": ca_file, "saved_ca": str(path)}


def _run_with_loading(parent, text, work, on_success, on_error, *, after_cancel=None):
    """Run ``work`` with a loading window over ``parent``.

    Signing in can take seconds -- a TLS handshake, the challenge, the
    server's password check -- and nothing on screen said anything was
    happening, so the button looked dead and was pressed again. Cancel stops
    the waiting, not the work: a device the server has already registered is
    still recorded through ``after_cancel``, so a second attempt does not
    register another one.
    """
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QWidget

    from .loading_dialog import close_loading_dialog, open_loading_dialog
    from .tasks import run_async

    state = {"cancelled": False, "dialog": None}

    def cancel():
        state["cancelled"] = True
        finish()

    def finish():
        dialog, state["dialog"] = state["dialog"], None
        if dialog is not None:
            close_loading_dialog(dialog)

    if isinstance(parent, QWidget):
        state["dialog"] = open_loading_dialog(parent, "Job Server", text, cancel)

    def succeeded(result):
        finish()
        if not state["cancelled"]:
            on_success(result)
        elif after_cancel is not None:
            after_cancel(result)

    def failed(message):
        finish()
        if not state["cancelled"]:
            on_error(message)

    run_async(
        QThreadPool.globalInstance(),
        work,
        on_success=succeeded,
        on_error=failed,
        owner=parent,
    )


def show_ca_registration(parent, url, ca_field):
    from PySide6.QtWidgets import QMessageBox
    from .store import default_data_dir

    try:
        require_client()
        url = validate_server_url(url)
    except ValueError as exc:
        QMessageBox.warning(parent, "Job Server", str(exc))
        return
    destination = (
        "This CA will be added to the current Windows user's trusted roots."
        if os.name == "nt"
        else "This CA will be saved for this Job Server connection."
    )

    def done(result):
        ca_field.setText(result["ca_file"])
        QMessageBox.information(
            parent,
            "Job Server",
            "CA trusted. You can now sign in.\nSaved CA: " + result["saved_ca"],
        )

    def warn(message):
        QMessageBox.warning(parent, "Job Server", message)

    def fetched(result):
        pem, fingerprint = result
        if not confirm_ca_fingerprint(parent, fingerprint, destination):
            return
        _run_with_loading(
            parent,
            "Trusting the server's CA...",
            lambda: install_server_ca(url, pem, fingerprint, default_data_dir()),
            done,
            warn,
            # Already trusted by then; the field must still name it.
            after_cancel=lambda result: ca_field.setText(result["ca_file"]),
        )

    _run_with_loading(
        parent,
        f"Fetching the CA certificate from {url}...",
        lambda: fetch_ca_for_confirmation(url),
        fetched,
        warn,
    )


#: Statuses with which a server refuses a device credential it no longer knows:
#: revoked, its account disabled or deleted, or the server set up again.
_REJECTED_STATUSES = {401, 403, 404}


def _credential_rejected(exc):
    from .job_server_client.client import ClientError

    if isinstance(exc, ClientError):
        return exc.status in _REJECTED_STATUSES
    return isinstance(exc, ValueError) and "Server identity" in str(exc)


def _device_id(path):
    """The device a credential file belongs to, or None when it cannot be read."""
    try:
        from .job_server_client.credentials import load_key

        return load_key(path)["device_id"]
    except Exception:
        return None


def _forget_credential(path):
    """Delete a replaced credential, but only one Job Manager created itself."""
    from .store import default_data_dir

    try:
        path = Path(path).resolve()
        if path.parent == (Path(default_data_dir()) / "identity").resolve():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def show_enrollment(
    parent, url, key_field, ca_file, on_done=None, *, allow_lan_http=False, again=False
):
    """Sign in with this host's device credential, or register a new one.

    ``again`` registers a new device with the username and password even when a
    credential exists, for a device the server has revoked or forgotten. The
    old credential is replaced only once the new one works, and the server is
    asked to revoke the old device, so nothing is left signed in by accident.
    A sign-in the server refuses offers the same, rather than leaving deleting
    and re-adding the host as the only way back.
    """
    from PySide6.QtWidgets import QMessageBox
    from .store import default_data_dir

    try:
        require_client()
        url = validate_server_url(url, allow_lan_http)
    except ValueError as exc:
        QMessageBox.warning(parent, "Job Server", str(exc))
        return
    text = key_field.text().strip()
    current = Path(text).expanduser() if text else None
    register = again or current is None or not current.exists()
    path = (
        Path(default_data_dir()) / "identity" / (str(uuid.uuid4()) + ".key")
        if again or current is None
        else current
    )
    previous = current if again and current is not None and current.exists() else None
    username = password = ""
    if register:
        credentials = server_credentials(parent)
        if credentials is None:
            return
        username, password = credentials
    state = {"rejected": False}

    def work():
        from .job_server_client.client import JobServerClient

        client = JobServerClient(url, path, ca_file or None, allow_lan_http=allow_lan_http)
        if not register:
            try:
                client.authenticate()
            except Exception as exc:
                state["rejected"] = _credential_rejected(exc)
                raise
            return client.request("GET", "/capabilities")
        client.enroll(username, password)
        old_device = _device_id(previous) if previous else None
        if old_device:
            try:
                client.request("POST", f"/me/devices/{quote(old_device, safe='')}/revoke")
            except Exception:
                pass  # Already revoked, or a server set up again: nothing to revoke.
        return client.request("GET", "/capabilities")

    def done(capabilities):
        recorded(capabilities)
        QMessageBox.information(
            parent,
            "Job Server",
            "Signed in again. This device is registered anew and the old credential was replaced."
            if again
            else "Device authenticated. Subsequent connections use this credential file.",
        )

    def recorded(capabilities):
        key_field.setText(str(path))
        if previous is not None and previous != path:
            _forget_credential(previous)
        if on_done:
            on_done(capabilities)

    def failed(message):
        if (
            state["rejected"]
            and QMessageBox.question(
                parent,
                "Job Server",
                f"The server no longer accepts this device's sign-in:\n{message}\n\n"
                "It may have been revoked, or the server set up again. Sign in again with "
                "your username and password?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            show_enrollment(
                parent,
                url,
                key_field,
                ca_file,
                on_done,
                allow_lan_http=allow_lan_http,
                again=True,
            )
            return
        QMessageBox.warning(parent, "Job Server", message)

    _run_with_loading(
        parent,
        f"Registering this device with {url}..." if register else f"Signing in to {url}...",
        work,
        done,
        failed,
        after_cancel=recorded,
    )


class JobServerBackend:
    def __init__(self, host):
        require_client()
        from .job_server_client.client import JobServerClient

        self.host = host
        self.client = JobServerClient(
            validate_server_url(host.server_url, host.server_allow_lan_http),
            host.server_key_path,
            host.server_ca_file or None,
            host.server_id or None,
            reuse_auth=True,
            allow_lan_http=host.server_allow_lan_http,
        )

    def submit(self, preset, job, local_files, after_id=None):
        if (
            preset.modules
            or preset.extra_directives
            or preset.submit_options
            or preset.nodes != 1
            or preset.ntasks != 1
        ):
            raise ValueError(
                "Job Server does not accept HPC modules, directives, submit options, nodes or tasks"
            )
        if job.remote_dir_provided or job.remote_input or job.start_after or job.force_run:
            raise ValueError(
                "Job Server does not accept existing work directories, start times or force-run"
            )
        from .schedulers.base import parse_memory_mb

        if preset.walltime:
            parts = preset.walltime.split(":")
            if len(parts) != 3 or not all(part.isdecimal() for part in parts):
                raise ValueError("Use HH:MM:SS walltime")
            hours, minutes, seconds = map(int, parts)
        else:
            hours, minutes, seconds = 0, 0, 0
        memory = parse_memory_mb(preset.memory) if preset.memory else 512
        if memory <= 0:
            raise ValueError("Invalid memory request")
        names = [Path(path).name for path in local_files]
        if len(set(name.casefold() for name in names)) != len(names):
            raise ValueError("Input files must have distinct basenames")
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, job.id))
        spec = {
            "name": job.name,
            # The job's own script is the only place a server host can set up its
            # environment, so its login commands run there, once for each job.
            "command": "\n".join(
                [*(self.host.login_commands or []), *preset.pre_commands, preset.command_template]
            ),
            "resources": {
                "cpus_per_task": preset.cpus_per_task,
                "memory_mb": memory,
                "walltime_seconds": hours * 3600 + minutes * 60 + seconds,
            },
            "fetch_globs": preset.fetch_globs,
            "auto_download": job.auto_download,
            "after_job": after_id,
            "chain_any": job.chain_any,
        }
        result = self.client.submit(spec, dict(zip(names, local_files)), key)
        job.server_job_id = result["id"]
        job.server_id = self.client.server_id
        job.owner_user_id = result["owner_id"]
        job.execution_os = self.client.request("GET", "/capabilities")["execution_os"]
        job.last_server_state = result["state"]
        job.remote_job_id = result["id"]
        job.remote_dir = result["id"]
        job.command = spec["command"]
        job.log_file = "job.log"
        import time

        job.submitted_at = time.time()
        job.touch(result["state"])
        return job

    def poll(self, jobs):
        pending = {job.remote_job_id for job in jobs}
        records = {}
        offset = 0
        while pending:
            page = self.client.request("GET", f"/jobs?limit=1000&offset={offset}")
            for record in page:
                if record["id"] in pending:
                    records[record["id"]] = record
                    pending.remove(record["id"])
            if len(page) < 1000:
                break
            offset += len(page)
        result = {}
        for job in jobs:
            remote = records.get(job.remote_job_id) or self.client.request(
                "GET", "/jobs/" + job.remote_job_id
            )
            job.rc = remote["exit_code"]
            job.last_error = remote.get("reason", "")
            job.last_server_state = remote["state"]
            ahead = remote.get("queue_ahead")
            # An older server sends nothing; bool is an int and is not a count.
            job.server_queue_ahead = (
                ahead if isinstance(ahead, int) and not isinstance(ahead, bool) else -1
            )
            result[job.id] = remote["state"]
        return result

    def cancel(self, job):
        return self.client.request("POST", f"/jobs/{job.remote_job_id}/cancel")

    def tail(self, job, lines=100):
        info = self.client.request("GET", f"/jobs/{job.remote_job_id}/log?limit=1")
        offset = max(0, info.get("size", 0) - 1048576)
        result = self.client.request(
            "GET", f"/jobs/{job.remote_job_id}/log?offset={offset}&limit=1048576"
        )
        return "\n".join(result["text"].splitlines()[-lines:])

    def files(self, job):
        return self.client.request("GET", f"/jobs/{job.remote_job_id}/files")

    def list_path(self, path, depth=1):
        from .server_files import list_path

        return list_path(self.client, path, depth)

    def stat_path(self, path, digest=True):
        from .server_files import stat_path

        return stat_path(self.client, path, digest)

    def download_paths(self, paths, local_dir, overwrite=False):
        from .server_files import download_paths

        return download_paths(self.client, paths, local_dir, overwrite)

    def tail_file(self, job, relative, lines=200):
        if relative in ("", "job.log"):
            return self.tail(job, lines)
        validate_path(relative)
        entry = next((item for item in self.files(job) if item["path"] == relative), None)
        if entry is None:
            raise ValueError("File is not in the server manifest")
        if not entry["size"]:
            return ""
        offset = max(0, entry["size"] - 1048576)
        data = self.client.request(
            "GET",
            f"/jobs/{job.remote_job_id}/files/" + quote(relative, safe="/"),
            headers={"Range": f"bytes={offset}-"},
            binary=True,
        )
        return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])

    def fetch_file(self, job, relative, target):
        return self.client.download(job.remote_job_id, relative, target)

    def fetch(self, job, directory, patterns):
        from .result_files import matches_pattern

        directory = Path(directory)
        patterns = [pattern.strip() for pattern in patterns or [] if pattern.strip()]
        protected = {os.path.normcase(os.path.realpath(path)) for path in job.input_files if path}
        fetched = []
        for entry in self.files(job):
            relative = validate_path(entry["path"])
            if not patterns or any(matches_pattern(relative, pattern) for pattern in patterns):
                target = directory / relative
                if not target.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("Result escapes download directory")
                if os.path.normcase(os.path.realpath(target)) in protected:
                    continue
                self.fetch_file(job, relative, target)
                fetched.append(str(target))
        return fetched
