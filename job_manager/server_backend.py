"""High-level backend contract for Job Manager; no remote shell transport."""

import fnmatch
import uuid
from pathlib import Path
import importlib.util
import re
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlsplit
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


def validate_server_url(url):
    parts = urlsplit(url)
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("Use a Job Server origin without credentials, paths or query strings")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("Invalid server port")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("HTTPS is required for LAN/Tailscale; HTTP is allowed only on localhost")
    return url.rstrip("/")


def backend_for(host):
    return JobServerBackend(host)


def show_enrollment(parent, url, key_field, ca_file, on_done=None):
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox
    from .tasks import run_async
    from .store import default_data_dir

    try:
        require_client()
        url = validate_server_url(url)
    except ValueError as exc:
        QMessageBox.warning(parent, "Job Server", str(exc))
        return
    path = (
        Path(key_field.text().strip()).expanduser()
        if key_field.text().strip()
        else Path(default_data_dir()) / "identity" / (str(uuid.uuid4()) + ".key")
    )
    username = password = ""
    if not path.exists():
        username, ok = QInputDialog.getText(parent, "Register server device", "Username")
        if not ok:
            return
        password, ok = QInputDialog.getText(
            parent, "Register server device", "Initial password", QLineEdit.EchoMode.Password
        )
        if not ok:
            return

    def work():
        from .job_server_client.client import JobServerClient

        client = JobServerClient(url, path, ca_file or None)
        if path.exists():
            client.authenticate()
        else:
            client.enroll(username, password)
        return client.request("GET", "/capabilities")

    def done(capabilities):
        key_field.setText(str(path))
        if on_done:
            on_done(capabilities)
        QMessageBox.information(
            parent,
            "Job Server",
            "Device authenticated. Subsequent connections use this credential file.",
        )

    run_async(
        QThreadPool.globalInstance(),
        work,
        on_success=done,
        on_error=lambda message: QMessageBox.warning(parent, "Job Server", message),
        owner=parent,
    )


class JobServerBackend:
    def __init__(self, host):
        require_client()
        from .job_server_client.client import JobServerClient

        self.host = host
        self.client = JobServerClient(
            validate_server_url(host.server_url),
            host.server_key_path,
            host.server_ca_file or None,
            host.server_id or None,
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

        parts = preset.walltime.split(":")
        if len(parts) != 3 or not all(part.isdecimal() for part in parts):
            raise ValueError("Use HH:MM:SS walltime")
        hours, minutes, seconds = map(int, parts)
        memory = parse_memory_mb(preset.memory) if preset.memory else 512
        if memory <= 0:
            raise ValueError("Invalid memory request")
        names = [Path(path).name for path in local_files]
        if len(set(name.casefold() for name in names)) != len(names):
            raise ValueError("Input files must have distinct basenames")
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, job.id))
        spec = {
            "name": job.name,
            "command": "\n".join([*preset.pre_commands, preset.command_template]),
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

    def tail_file(self, job, relative, lines=200):
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
        directory = Path(directory)
        fetched = []
        for entry in self.files(job):
            relative = validate_path(entry["path"])
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
                target = directory / relative
                if not target.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("Result escapes download directory")
                self.fetch_file(job, relative, target)
                fetched.append(str(target))
        return fetched
