"""Request handling for the local job API: routing, validation, serialisation.

Deliberately free of Qt and of sockets, so the whole contract can be exercised
by the headless CI job that installs only pytest. :mod:`job_manager.api_server`
adds the HTTP transport and the hop onto the GUI thread; everything about
*what* a request means lives here.

The service methods this calls are the same ones the wizard calls. An API
submission is not a second submission path -- it builds a
:class:`~job_manager.models.SubmitPreset` and hands it to ``JobService.submit``,
so a job that arrives over the socket is indistinguishable from one the user
typed in, and every later step (polling, chaining, download, notification) is
the code that was already there.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .models import ACTIVE_STATES, TERMINAL_STATES, HostProfile, Job, SubmitPreset

#: Bumped when a response or a request field changes meaning. The path carries
#: it, so a client written against v1 keeps working when v2 appears beside it.
API_VERSION = 1
API_PREFIX = f"/api/v{API_VERSION}"

#: Persistent shared secret. Separate from the endpoint file, which is deleted
#: when the server stops: a client configured once with the token must not have
#: to be reconfigured because MoleditPy was restarted.
TOKEN_FILENAME = "api_token"
#: Written while the server is listening, removed when it stops. This is how a
#: client finds the port without being told one.
ENDPOINT_FILENAME = "api.json"

DEFAULT_PORT = 8765
#: Loopback only, and not configurable. See docs/API.md: the token is readable
#: by anything running as this user, so it is not a credential that would make
#: exposing the port to a network safe.
BIND_HOST = "127.0.0.1"

#: How long a request that has to reach the cluster (a log tail, a directory
#: listing) may take before the client is told so, rather than hanging.
REMOTE_TIMEOUT = 120.0

#: Names a submission may set on the preset it builds. Anything else in the
#: body is either a job field handled explicitly or a mistake worth reporting.
PRESET_FIELDS = {
    "queue": str,
    "account": str,
    "walltime": str,
    "nodes": int,
    "ntasks": int,
    "cpus_per_task": int,
    "memory": str,
    "modules": list,
    "pre_commands": list,
    "extra_directives": list,
    "fetch_globs": list,
    "auto_download": bool,
}


class ApiError(Exception):
    """A request that cannot be served, carrying the status the client gets."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)

    def payload(self) -> Dict[str, Any]:
        return {"error": self.message, "status": self.status}


class Deferred:
    """A reply that is not ready when the handler returns.

    A log tail has to reach the host, and the handler runs on the GUI thread --
    which is also the thread the answer arrives on, so waiting there would
    deadlock. The handler returns one of these instead and the socket thread,
    which has nothing else to do, waits on it.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._value: Any = None
        self._error: Optional[ApiError] = None

    def set_result(self, value: Any) -> None:
        self._value = value
        self._event.set()

    def set_error(self, message: str, status: int = 502) -> None:
        self._error = ApiError(status, str(message))
        self._event.set()

    def wait(self, timeout: float = REMOTE_TIMEOUT) -> Any:
        if not self._event.wait(timeout):
            raise ApiError(504, f"The host did not answer within {int(timeout)} s")
        if self._error is not None:
            raise self._error
        return self._value


# --- the token and the endpoint file ----------------------------------------


def token_path(directory: str) -> str:
    return os.path.join(directory, TOKEN_FILENAME)


def endpoint_path(directory: str) -> str:
    return os.path.join(directory, ENDPOINT_FILENAME)


def write_private_file(path: str, text: str) -> None:
    """Write a file only this user can read.

    The mode is applied when the temp file is *created*, before any content is
    written, so the secret is never on disk world-readable even for an instant.
    It is a no-op on Windows, where the file inherits the directory's ACL --
    said plainly in docs/API.md rather than pretended otherwise.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = f"{path}.tmp{os.getpid()}"
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    os.replace(temp, path)


def read_token(directory: str) -> str:
    try:
        with open(token_path(directory), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def new_token(length: int = 32) -> str:
    """A secret that is safe to hand to a command line.

    ``token_urlsafe`` draws from the base64url alphabet, so about one token in
    sixty-four begins with "-". Every one of those breaks
    ``--token <value>``: argparse reads the leading hyphen as an option name
    and refuses with "expected one argument", which says nothing about the
    real problem and cannot be worked around without knowing to write
    ``--token=<value>`` instead. Rerolling costs nothing and the entropy is
    unchanged -- the first character is simply drawn from a smaller set.
    """
    while True:
        token = secrets.token_urlsafe(length)
        if not token.startswith("-"):
            return token


def ensure_token(directory: str, renew: bool = False) -> str:
    """The shared secret, generating and storing one on first use."""
    existing = "" if renew else read_token(directory)
    if existing:
        return existing
    token = new_token(32)
    write_private_file(token_path(directory), token + "\n")
    return token


def write_endpoint_file(directory: str, port: int, token: str) -> str:
    """Publish where the server is listening, for a client to discover."""
    path = endpoint_path(directory)
    write_private_file(
        path,
        json.dumps(
            {
                "url": f"http://{BIND_HOST}:{int(port)}{API_PREFIX}",
                "host": BIND_HOST,
                "port": int(port),
                "token": token,
                "api_version": API_VERSION,
                "pid": os.getpid(),
                "started_at": time.time(),
            },
            indent=2,
        )
        + "\n",
    )
    return path


def remove_endpoint_file(directory: str) -> None:
    try:
        os.unlink(endpoint_path(directory))
    except OSError:
        pass


def tokens_match(presented: str, expected: str) -> bool:
    """Constant-time comparison; a token is a secret like any other."""
    if not presented or not expected:
        return False
    return secrets.compare_digest(str(presented), str(expected))


# --- serialisation ----------------------------------------------------------


def host_payload(host: HostProfile) -> Dict[str, Any]:
    """A host as the API describes it: enough to submit to, and no more.

    Not ``to_dict()``: ssh options and login commands are this machine's
    business, and a client only ever needs to name the host and know what it
    will be submitting into.
    """
    return {
        "id": host.id,
        "name": host.name,
        "target": host.target,
        "scheduler": host.scheduler,
        "backend": host.backend,
        "enabled": bool(host.enabled),
        "remote_root": host.remote_root,
        "is_local": bool(host.is_local),
        "max_concurrent": int(host.max_concurrent),
    }


def preset_payload(preset: SubmitPreset) -> Dict[str, Any]:
    data = preset.to_dict()
    #: The submit body spells it "command"; a preset read back has to use the
    #: same word or a client cannot round-trip one into the other.
    data["command"] = preset.command_template
    return data


def job_payload(job: Job, store: Any = None) -> Dict[str, Any]:
    """A job record plus the things a client would otherwise have to derive."""
    data = job.to_dict()
    data["active"] = bool(job.is_active)
    data["terminal"] = bool(job.is_terminal)
    data["elapsed_seconds"] = round(job.elapsed(), 3)
    data["waiting_seconds"] = round(job.waiting(), 3)
    if store is not None:
        blocker = store.chain_blocker(job)
        data["blocked_by"] = blocker.id if blocker is not None else ""
    return data


# --- the API ----------------------------------------------------------------


class JobApi:
    """Turns a parsed request into a call on :class:`JobService`.

    Every method here runs on the GUI thread (the server marshals it), because
    the store has exactly one writer thread and always has.
    """

    def __init__(self, service: Any) -> None:
        self.service = service

    @property
    def store(self) -> Any:
        return self.service.store

    # --- routing ------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """``(status, payload)``; the payload may be a :class:`Deferred`.

        Raises :class:`ApiError` for anything the client got wrong.
        """
        query = dict(query or {})
        body = dict(body or {})
        parts = [p for p in (path or "").strip("/").split("/") if p]
        prefix = [p for p in API_PREFIX.strip("/").split("/") if p]
        if parts[: len(prefix)] != prefix:
            raise ApiError(
                404,
                f"Unknown path '{path}'. Every route is under {API_PREFIX}/ "
                f"(try {API_PREFIX}/ping).",
            )
        parts = parts[len(prefix) :]
        method = (method or "GET").upper()

        if not parts:
            parts = ["ping"]
        head, rest = parts[0], parts[1:]

        if head == "ping" and not rest:
            return self._require("GET", method, self.ping)
        if head == "hosts" and not rest:
            return self._require("GET", method, self.hosts)
        if head == "presets" and not rest:
            return self._require("GET", method, lambda: self.presets(query))
        if head == "jobs":
            return self._jobs_route(method, rest, query, body)
        raise ApiError(404, f"Unknown path '{path}'")

    def _jobs_route(
        self,
        method: str,
        rest: List[str],
        query: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> Tuple[int, Any]:
        if not rest:
            if method == "GET":
                return 200, self.list_jobs(query)
            if method == "POST":
                return 202, self.submit(body)
            raise ApiError(405, "GET to list jobs, POST to submit one")
        job_id, action = rest[0], (rest[1] if len(rest) > 1 else "")
        if not action:
            if method == "GET":
                return 200, {"job": job_payload(self._job(job_id), self.store)}
            if method == "DELETE":
                return 200, self.forget(job_id)
            raise ApiError(405, "GET to read a job, DELETE to stop tracking it")
        if action == "cancel":
            return self._require("POST", method, lambda: self.cancel(job_id, body))
        if action == "download":
            return self._require("POST", method, lambda: self.download(job_id, body))
        if action == "log":
            return self._require("GET", method, lambda: self.log(job_id, query))
        if action == "files":
            return self._require("GET", method, lambda: self.files(job_id))
        raise ApiError(404, f"Unknown job action '{action}'")

    @staticmethod
    def _require(expected: str, method: str, run: Callable[[], Any]) -> Tuple[int, Any]:
        if method != expected:
            raise ApiError(405, f"Use {expected} here, not {method}")
        return 200, run()

    # --- reads --------------------------------------------------------------

    def ping(self) -> Dict[str, Any]:
        from . import PLUGIN_VERSION

        jobs = list(self.store.jobs.values())
        return {
            "ok": True,
            "plugin": "Job Manager",
            "plugin_version": PLUGIN_VERSION,
            "api_version": API_VERSION,
            "jobs": len(jobs),
            "active_jobs": sum(1 for job in jobs if job.is_active),
            "hosts": len(self.store.hosts),
        }

    def hosts(self) -> Dict[str, Any]:
        return {"hosts": [host_payload(host) for host in self.store.host_list()]}

    def presets(self, query: Mapping[str, str]) -> Dict[str, Any]:
        wanted = str(query.get("host", "") or "")
        if wanted:
            presets = self.store.presets_for_host(self._host(wanted).id)
        else:
            presets = sorted(self.store.presets.values(), key=lambda p: p.name.lower())
        return {"presets": [preset_payload(preset) for preset in presets]}

    def list_jobs(self, query: Mapping[str, str]) -> Dict[str, Any]:
        jobs = self.store.job_list()
        state = str(query.get("state", "") or "").upper()
        if state == "ACTIVE":
            jobs = [job for job in jobs if job.state in ACTIVE_STATES]
        elif state == "TERMINAL":
            jobs = [job for job in jobs if job.state in TERMINAL_STATES]
        elif state:
            jobs = [job for job in jobs if job.state == state]
        host = str(query.get("host", "") or "")
        if host:
            host_id = self._host(host).id
            jobs = [job for job in jobs if job.host_id == host_id]
        name = str(query.get("name", "") or "").lower()
        if name:
            jobs = [job for job in jobs if name in job.name.lower()]
        if query.get("limit"):
            jobs = jobs[: self._int(query.get("limit"), "limit", minimum=1)]
        return {"jobs": [job_payload(job, self.store) for job in jobs]}

    # --- writes -------------------------------------------------------------

    def submit(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        """Create and start a job from a request body. See docs/API.md."""
        host = self._host(str(body.get("host", "") or ""))
        if not host.enabled:
            raise ApiError(409, f"Host '{host.name}' is disabled in the Hosts dialog")
        files = self._files(body)
        remote_dir = str(body.get("remote_dir", "") or "").strip()
        remote_input = str(body.get("remote_input", "") or "").strip()
        if not files and not remote_dir:
            raise ApiError(
                400,
                "Nothing to run: give 'files' (local inputs to upload) or "
                "'remote_dir' (a directory already on the host).",
            )
        if remote_input and not remote_dir:
            raise ApiError(400, "'remote_input' names a file inside 'remote_dir', which is unset")
        preset = self._preset(host, body)
        after_job = None
        after_id = str(body.get("after_job", "") or "").strip()
        if after_id:
            after_job = self._job(after_id)
            if after_job.host_id != host.id:
                raise ApiError(
                    400,
                    f"'{after_job.name}' runs on a different host, and a job can "
                    f"only be chained behind one on the same host.",
                )
        job = self.service.submit(
            host,
            preset,
            str(body.get("name", "") or ""),
            files,
            auto_download=self._optional_bool(body, "auto_download", preset.auto_download),
            after_job=after_job,
            start_after=self._start_after(body),
            chain_any=self._optional_bool(body, "chain_any", False),
            remote_dir=remote_dir,
            remote_input=remote_input,
        )
        return {"job": job_payload(job, self.store)}

    def cancel(self, job_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        job = self._job(job_id)
        if job.is_terminal:
            raise ApiError(409, f"{job.name} has already finished ({job.state})")
        self.service.cancel(
            job, release_dependents=self._optional_bool(body, "release_dependents", True)
        )
        return {"job": job_payload(job, self.store), "cancelling": True}

    def download(self, job_id: str, body: Mapping[str, Any]) -> Any:
        job = self._job(job_id)
        names = body.get("names")
        if names is not None and not isinstance(names, (list, tuple)):
            raise ApiError(400, "'names' must be a list of remote file names")
        into = str(body.get("into", "") or "")
        if into and not os.path.isdir(into):
            raise ApiError(400, f"'{into}' is not a directory on this machine")
        if self._optional_bool(body, "wait", False):
            return self._download_and_wait(job, into, names)
        if not self.service.download(job, into=into, names=list(names) if names else None):
            raise ApiError(409, f"A download is already running for {job.name}")
        return {"job": job_payload(job, self.store), "downloading": True}

    def _download_and_wait(self, job: Job, into: str, names: Any) -> Deferred:
        """``wait: true`` -- answer with the files, once they are actually here.

        A script that downloads and then reads the output needs to know the
        write has finished; polling the job record cannot tell it that, since
        the state returns to what it was before the download started.
        """
        deferred = Deferred()
        job_id = job.id

        def disconnect() -> None:
            for signal, slot in (
                (self.service.results_ready, on_ready),
                (self.service.error, on_error),
            ):
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass

        def on_ready(finished_id: str, paths: Sequence[str]) -> None:
            if finished_id != job_id:
                return
            disconnect()
            current = self._job_or_none(job_id) or job
            deferred.set_result({"job": job_payload(current, self.store), "files": list(paths)})

        def on_error(message: str) -> None:
            disconnect()
            deferred.set_error(message)

        self.service.results_ready.connect(on_ready)
        self.service.error.connect(on_error)
        if not self.service.download(job, into=into, names=list(names) if names else None):
            disconnect()
            raise ApiError(409, f"A download is already running for {job.name}")
        return deferred

    def forget(self, job_id: str) -> Dict[str, Any]:
        job = self._job(job_id)
        if job.is_active:
            raise ApiError(
                409,
                f"{job.name} is still {job.state}. Cancel it first, or it would "
                f"go on running on the host untracked.",
            )
        self.service.remove_job(job.id)
        return {"removed": job.id}

    # --- reads that have to reach the host ----------------------------------

    def log(self, job_id: str, query: Mapping[str, str]) -> Deferred:
        job = self._job(job_id)
        lines = self._int(query.get("lines"), "lines", minimum=1) if query.get("lines") else 200
        # Empty means the job's own log; tail_file resolves that the same way
        # the monitor's Tail Log button does.
        filename = str(query.get("file", "") or "") or job.log_file
        deferred = Deferred()
        self.service.tail_file(
            job,
            filename,
            lines,
            on_done=lambda text: deferred.set_result({"text": text, "file": filename}),
            on_error=deferred.set_error,
        )
        return deferred

    def files(self, job_id: str) -> Deferred:
        job = self._job(job_id)
        if not job.remote_dir:
            raise ApiError(409, f"{job.name} has no remote directory yet")
        deferred = Deferred()
        self.service.list_remote_results(
            job,
            lambda names: deferred.set_result({"files": list(names), "remote_dir": job.remote_dir}),
            deferred.set_error,
        )
        return deferred

    # --- resolution helpers -------------------------------------------------

    def _host(self, wanted: str) -> HostProfile:
        """A host by id or by name; the name is what a script would name."""
        if not wanted:
            raise ApiError(400, "'host' is required: the id or name of a configured host")
        host = self.store.hosts.get(wanted)
        if host is not None:
            return host
        matches = [h for h in self.store.hosts.values() if h.name.lower() == wanted.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ApiError(409, f"More than one host is named '{wanted}'; use its id instead")
        known = ", ".join(sorted(h.name for h in self.store.hosts.values())) or "none configured"
        raise ApiError(404, f"No host called '{wanted}'. Known hosts: {known}")

    def _job(self, job_id: str) -> Job:
        job = self._job_or_none(job_id)
        if job is None:
            raise ApiError(404, f"No tracked job with id '{job_id}'")
        return job

    def _job_or_none(self, job_id: str) -> Optional[Job]:
        return self.store.jobs.get(str(job_id or ""))

    @staticmethod
    def _files(body: Mapping[str, Any]) -> List[str]:
        # Imported here, not at the top: this module is deliberately free of
        # everything but the standard library and .models, and runner pulls in
        # the schedulers.
        from .runner import check_input_name

        raw = body.get("files") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raise ApiError(400, "'files' must be a path or a list of paths")
        files: List[str] = []
        for entry in raw:
            # Absolute, because the server's working directory is MoleditPy's
            # and has nothing to do with the caller's.
            path = os.path.abspath(os.path.expanduser(str(entry)))
            if not os.path.isfile(path):
                raise ApiError(400, f"No such input file: {entry}")
            # Refused here as well as in the runner, so a caller is told what
            # is wrong with its request instead of watching a job fail.
            try:
                check_input_name(os.path.basename(path))
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            files.append(path)
        return files

    def _preset(self, host: HostProfile, body: Mapping[str, Any]) -> SubmitPreset:
        """The resource request, from a named preset and/or explicit fields.

        A submission with neither is refused rather than falling back to the
        dataclass default, whose command template names one particular program:
        a caller that forgot the command would otherwise have silently run
        ORCA on whatever it uploaded.
        """
        named = str(body.get("preset", "") or "").strip()
        command = body.get("command")
        if named:
            # A copy: the stored preset must not pick up this call's overrides.
            preset = SubmitPreset.from_dict(self._named_preset(host, named).to_dict())
            preset.id = SubmitPreset().id
        elif command:
            preset = SubmitPreset(host_id=host.id, name="api", command_template="")
        else:
            raise ApiError(
                400,
                "Give 'command' (the command line to run) or 'preset' (the name "
                "of a saved preset for this host).",
            )
        preset.host_id = host.id
        if command is not None:
            preset.command_template = str(command)
        for key, kind in PRESET_FIELDS.items():
            if key in body:
                setattr(preset, key, self._coerce(body[key], key, kind))
        if not preset.command_template.strip():
            raise ApiError(400, "The command line is empty")
        return preset

    def _named_preset(self, host: HostProfile, named: str) -> SubmitPreset:
        preset = self.store.presets.get(named)
        if preset is not None and preset.host_id == host.id:
            return preset
        for_host = self.store.presets_for_host(host.id)
        matches = [p for p in for_host if p.name.lower() == named.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ApiError(409, f"More than one preset on '{host.name}' is named '{named}'")
        known = ", ".join(p.name for p in for_host) or "none"
        raise ApiError(404, f"No preset '{named}' on host '{host.name}'. Known presets: {known}")

    @staticmethod
    def _coerce(value: Any, key: str, kind: type) -> Any:
        if kind is bool:
            if not isinstance(value, bool):
                raise ApiError(400, f"'{key}' must be true or false")
            return value
        if kind is int:
            # bool is an int in Python; a client sending true for 'nodes' has
            # made a mistake worth reporting rather than reading as 1.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ApiError(400, f"'{key}' must be a whole number")
            if value < 0:
                raise ApiError(400, f"'{key}' cannot be negative")
            return value
        if kind is list:
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                raise ApiError(400, f"'{key}' must be a list of strings")
            return [str(entry) for entry in value]
        return str(value)

    @staticmethod
    def _optional_bool(body: Mapping[str, Any], key: str, default: bool) -> bool:
        if key not in body:
            return bool(default)
        value = body[key]
        if not isinstance(value, bool):
            raise ApiError(400, f"'{key}' must be true or false")
        return value

    @staticmethod
    def _int(value: Any, key: str, minimum: Optional[int] = None) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, f"'{key}' must be a whole number") from exc
        if minimum is not None and number < minimum:
            raise ApiError(400, f"'{key}' must be at least {minimum}")
        return number

    @staticmethod
    def _start_after(body: Mapping[str, Any]) -> float:
        """``start_after`` as an epoch second, accepting either spelling.

        A number is one already; a string is a local time, which is what a
        person writes into a script and what the wizard's own field shows.
        """
        value = body.get("start_after", 0)
        if not value:
            return 0.0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        text = str(value).strip().replace("Z", "")
        for shape in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                return time.mktime(time.strptime(text, shape))
            except ValueError:
                continue
        raise ApiError(
            400,
            "'start_after' must be an epoch second or a local time like 2026-01-31T18:30",
        )


__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "BIND_HOST",
    "DEFAULT_PORT",
    "ENDPOINT_FILENAME",
    "PRESET_FIELDS",
    "REMOTE_TIMEOUT",
    "TOKEN_FILENAME",
    "ApiError",
    "Deferred",
    "JobApi",
    "endpoint_path",
    "ensure_token",
    "new_token",
    "write_private_file",
    "host_payload",
    "job_payload",
    "preset_payload",
    "read_token",
    "remove_endpoint_file",
    "token_path",
    "tokens_match",
    "write_endpoint_file",
]
