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

import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote

from .api_inputs import ApiInputs
from .api_security import (
    endpoint_path as endpoint_path,
)
from .api_security import (
    ensure_token as ensure_token,
)
from .api_security import (
    live_endpoint as live_endpoint,
)
from .api_security import (
    new_token as new_token,
)
from .api_security import (
    read_token as read_token,
)
from .api_security import (
    remove_endpoint_file as remove_endpoint_file,
)
from .api_security import (
    token_path as token_path,
)
from .api_security import (
    tokens_match as tokens_match,
)
from .api_security import (
    write_endpoint_file as write_endpoint_file,
)
from .api_security import (
    write_private_file as write_private_file,
)
from .api_types import (
    API_PREFIX as API_PREFIX,
)
from .api_types import (
    API_VERSION as API_VERSION,
)
from .api_types import (
    BIND_HOST as BIND_HOST,
)
from .api_types import (
    DEFAULT_PORT as DEFAULT_PORT,
)
from .api_types import (
    DOWNLOAD_TIMEOUT as DOWNLOAD_TIMEOUT,
)
from .api_types import (
    ENDPOINT_FILENAME as ENDPOINT_FILENAME,
)
from .api_types import (
    PRESET_FIELDS as PRESET_FIELDS,
)
from .api_types import (
    REMOTE_TIMEOUT as REMOTE_TIMEOUT,
)
from .api_types import (
    ROUTES as ROUTES,
)
from .api_types import (
    TOKEN_FILENAME as TOKEN_FILENAME,
)
from .api_types import (
    ApiError as ApiError,
)
from .api_types import (
    Deferred as Deferred,
)
from .api_types import (
    route_list as route_list,
)
from .models import (
    ACTIVE_STATES,
    SCHEDULER_SHELL,
    SCHEDULER_WINDOWS,
    STATE_DOWNLOADING,
    STATE_LOST,
    STATE_UPLOADING,
    TERMINAL_STATES,
    HostProfile,
    Job,
    SubmitPreset,
)

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
        # Unquoted per segment, after the split: a host is addressed by its
        # name, and a name may have a space in it.
        parts = [unquote(p) for p in (path or "").strip("/").split("/") if p]
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
        if head == "hosts" and len(rest) == 2:
            return self._host_route(method, rest[0], rest[1], query, body)
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
        if action == "force":
            return self._require("POST", method, lambda: self.force(job_id))
        if action == "recheck":
            return self._require("POST", method, lambda: self.recheck(job_id))
        raise ApiError(404, f"Unknown job action '{action}'")

    def _host_route(
        self,
        method: str,
        host_ref: str,
        action: str,
        query: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> Tuple[int, Any]:
        if action == "status":
            return self._require("GET", method, lambda: self.host_status(host_ref, query))
        if action == "files":
            return self._require("GET", method, lambda: self.host_files(host_ref, query))
        if action == "file":
            return self._require("GET", method, lambda: self.host_file(host_ref, query))
        if action == "download":
            return self._require("POST", method, lambda: self.host_download(host_ref, body))
        raise ApiError(404, f"Unknown host action '{action}'")

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
            "routes": route_list(),
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
        from .input_names import check_upload_names

        try:
            check_upload_names(files, windows=host.scheduler == SCHEDULER_WINDOWS)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
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
        force_run = self._optional_bool(body, "force_run", False)
        if force_run:
            self._check_forceable(host, body)
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
            force_run=force_run,
        )
        return {"job": job_payload(job, self.store)}

    @staticmethod
    def _check_forceable(host: HostProfile, body: Mapping[str, Any]) -> None:
        """``force_run`` is for a host with no queue system, and means "now"."""
        if host.scheduler not in (SCHEDULER_SHELL, SCHEDULER_WINDOWS):
            raise ApiError(
                400,
                f"'{host.name}' has a queue system ({host.scheduler}), which decides its own "
                "order: 'force_run' is for a host with no scheduler.",
            )
        if str(body.get("after_job", "") or "").strip():
            raise ApiError(400, "'force_run' starts a job now; it cannot also wait for 'after_job'")
        if body.get("start_after"):
            raise ApiError(
                400, "'force_run' starts a job now; it cannot also wait for 'start_after'"
            )

    def cancel(self, job_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        job = self._job(job_id)
        if job.is_terminal:
            raise ApiError(409, f"{job.name} has already finished ({job.state})")
        if not job.is_active:
            # UPLOADING has no queue id to cancel yet, and the submission
            # finishing afterwards would put the job on the host regardless --
            # the cancel would be reported and then silently undone.
            raise ApiError(
                409, f"{job.name} is {job.state}; cancel it once it has reached the queue"
            )
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
            # ``error`` is the service's one error channel, and carries no job
            # id: another job's failed poll or submission would otherwise answer
            # this request with its own message. Only this download ending does.
            in_flight = getattr(self.service, "download_in_flight", None)
            if in_flight is not None and in_flight(job_id):
                return
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
        if job.is_active or job.state in (STATE_UPLOADING, STATE_DOWNLOADING):
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

    def force(self, job_id: str) -> Deferred:
        job = self._job(job_id)
        refusal = self.service.force_refusal(job)
        if refusal:
            raise ApiError(409, refusal)
        deferred = Deferred()

        def done(current: Job, started: bool) -> None:
            if not started:
                deferred.set_error(
                    f"{current.name} is no longer waiting in the queue: "
                    "it has started or been cancelled.",
                    409,
                )
                return
            deferred.set_result({"job": job_payload(current, self.store), "forced": True})

        self.service.force_run(job, on_done=done, on_error=deferred.set_error)
        return deferred

    def recheck(self, job_id: str) -> Deferred:
        job = self._job(job_id)
        if job.state != STATE_LOST:
            raise ApiError(409, f"{job.name} is {job.state}; only a LOST job is re-checked")
        deferred = Deferred()

        def done(report: Dict[str, Any]) -> None:
            current = self._job_or_none(job.id) or job
            reply = {
                key: report.get(key)
                for key in (
                    "previous_state",
                    "state",
                    "changed",
                    "rc",
                    "sentinel",
                    "runner_status",
                    "files",
                )
            }
            reply["job"] = job_payload(current, self.store)
            deferred.set_result(reply)

        self.service.recheck(job, on_done=done, on_error=deferred.set_error)
        return deferred

    # --- the host itself ----------------------------------------------------

    def host_status(self, host_ref: str, query: Mapping[str, str]) -> Deferred:
        host = self._usable_host(host_ref)
        stats = self._query_flag(query, "stats", True)
        skipped = ""
        if stats and not host.monitor_usage:
            # The profile says this machine's load is not ours to sample --
            # a shared login node -- and a request over the API is no
            # different from the Host Monitor asking.
            stats = False
            skipped = "Load sampling is switched off for this host in the Hosts dialog."
        deferred = Deferred()

        def done(report: Dict[str, Any]) -> None:
            deferred.set_result(self._status_payload(host, report, skipped))

        self.service.host_status(host, done, deferred.set_error, stats=stats)
        return deferred

    def _status_payload(
        self, host: HostProfile, report: Mapping[str, Any], skipped: str
    ) -> Dict[str, Any]:
        tracked = [job for job in self.store.job_list() if job.host_id == host.id and job.is_active]
        detail = report.get("queue")
        places: Dict[str, Dict[str, Any]] = {}
        if detail is not None:
            for item in detail.get("running", []):
                known = self.store.jobs.get(item["job_id"])
                item["name"] = known.name if known is not None else ""
                places[item["job_id"]] = {"queue": "running"}
            for index, item in enumerate(detail.get("waiting", [])):
                known = self.store.jobs.get(item["job_id"])
                item["name"] = known.name if known is not None else ""
                item["position"] = index + 1
                item["ahead"] = index
                places[item["job_id"]] = {"queue": "waiting", "position": index + 1, "ahead": index}
            running = detail.get("running", [])
            queue: Dict[str, Any] = {
                "kind": "helper",
                "paused": bool(detail.get("paused")),
                "limits": dict(detail.get("limits", {})),
                "running": running,
                "waiting": detail.get("waiting", []),
                "cores_in_use": sum(item["cores"] for item in running),
                "memory_in_use_mb": sum(item["memory_mb"] for item in running),
            }
        elif host.scheduler in (SCHEDULER_SHELL, SCHEDULER_WINDOWS):
            queue = {"kind": "none"}
        else:
            queue = {"kind": "scheduler", "scheduler": host.scheduler}
        jobs = []
        for job in tracked:
            entry = {"id": job.id, "name": job.name, "state": job.state}
            entry.update(places.get(job.id, {}))
            jobs.append(entry)
        payload: Dict[str, Any] = {
            "host": host_payload(host),
            "stats": report.get("stats"),
            "queue": queue,
            "jobs": jobs,
        }
        if skipped:
            payload["stats_skipped"] = skipped
        return payload

    def host_files(self, host_ref: str, query: Mapping[str, str]) -> Deferred:
        host = self._usable_host(host_ref)
        path = self._remote_path(query.get("path"))
        depth = self._int(query.get("depth"), "depth", minimum=1) if query.get("depth") else 1
        if depth > 4:
            raise ApiError(400, "'depth' is at most 4")
        deferred = Deferred()
        self.service.list_host_path(
            host,
            path,
            depth,
            lambda names: deferred.set_result(
                {"path": path, "depth": depth, "entries": list(names)}
            ),
            deferred.set_error,
        )
        return deferred

    def host_file(self, host_ref: str, query: Mapping[str, str]) -> Deferred:
        host = self._usable_host(host_ref)
        path = self._remote_path(query.get("path"))
        digest = self._query_flag(query, "hash", True)
        deferred = Deferred()
        self.service.stat_host_path(host, path, digest, deferred.set_result, deferred.set_error)
        return deferred

    def host_download(self, host_ref: str, body: Mapping[str, Any]) -> Deferred:
        host = self._usable_host(host_ref)
        raw = body.get("paths", body.get("path"))
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ApiError(400, "'paths' must be a list of file paths on the host")
        paths = [self._remote_path(entry) for entry in raw]
        into = str(body.get("into", "") or "")
        if into and not os.path.isdir(into):
            raise ApiError(400, f"'{into}' is not a directory on this machine")
        overwrite = self._optional_bool(body, "overwrite", False)
        deferred = Deferred(timeout=DOWNLOAD_TIMEOUT)

        def done(result: Tuple[List[str], List[Tuple[str, str]], str]) -> None:
            downloaded, skipped, local_dir = result
            deferred.set_result(
                {
                    "into": local_dir,
                    "files": list(downloaded),
                    "skipped": [{"path": path, "reason": reason} for path, reason in skipped],
                }
            )

        self.service.download_host_paths(
            host, paths, into, done, deferred.set_error, overwrite=overwrite
        )
        return deferred

    def _usable_host(self, wanted: str) -> HostProfile:
        return ApiInputs(self.store)._usable_host(wanted)

    @staticmethod
    def _remote_path(value: Any) -> str:
        return ApiInputs._remote_path(value)

    @staticmethod
    def _query_flag(query: Mapping[str, str], key: str, default: bool) -> bool:
        return ApiInputs._query_flag(query, key, default)

    # --- resolution helpers -------------------------------------------------

    def _host(self, wanted: str) -> HostProfile:
        """A host by id or by name; the name is what a script would name."""
        return ApiInputs(self.store)._host(wanted)

    def _job(self, job_id: str) -> Job:
        return ApiInputs(self.store)._job(job_id)

    def _job_or_none(self, job_id: str) -> Optional[Job]:
        return ApiInputs(self.store)._job_or_none(job_id)

    @staticmethod
    def _files(body: Mapping[str, Any]) -> List[str]:
        return ApiInputs._files(body)

    def _preset(self, host: HostProfile, body: Mapping[str, Any]) -> SubmitPreset:
        """The resource request, from a named preset and/or explicit fields.

        A submission with neither is refused rather than falling back to the
        dataclass default, whose command template names one particular program:
        a caller that forgot the command would otherwise have silently run
        ORCA on whatever it uploaded.
        """
        return ApiInputs(self.store)._preset(host, body)

    def _named_preset(self, host: HostProfile, named: str) -> SubmitPreset:
        return ApiInputs(self.store)._named_preset(host, named)

    @staticmethod
    def _coerce(value: Any, key: str, kind: type) -> Any:
        return ApiInputs._coerce(value, key, kind)

    @staticmethod
    def _optional_bool(body: Mapping[str, Any], key: str, default: bool) -> bool:
        return ApiInputs._optional_bool(body, key, default)

    @staticmethod
    def _int(value: Any, key: str, minimum: Optional[int] = None) -> int:
        return ApiInputs._int(value, key, minimum)

    @staticmethod
    def _start_after(body: Mapping[str, Any]) -> float:
        """``start_after`` as an epoch second, accepting either spelling.

        A number is one already; a string is a local time, which is what a
        person writes into a script and what the wizard's own field shows.
        """
        return ApiInputs._start_after(body)


__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "BIND_HOST",
    "DEFAULT_PORT",
    "DOWNLOAD_TIMEOUT",
    "ENDPOINT_FILENAME",
    "PRESET_FIELDS",
    "REMOTE_TIMEOUT",
    "TOKEN_FILENAME",
    "ApiError",
    "Deferred",
    "JobApi",
    "endpoint_path",
    "live_endpoint",
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
