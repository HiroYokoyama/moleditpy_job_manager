"""Local API protocol constants, errors and deferred responses."""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

#: Bumped when a response or a request field changes meaning. The path carries
#: it, so a client written against v1 keeps working when v2 appears beside it.
API_VERSION = 1


API_PREFIX = f"/api/v{API_VERSION}"


#: Persistent shared secret. Separate from the endpoint file, which is deleted
#: when the server stops: a client configured once with the token must not have
#: to be reconfigured because MoleditPy was restarted.
#: Written while the server is listening, removed when it stops. This is how a
#: client finds the port without being told one.
TOKEN_FILENAME = "api_token"


ENDPOINT_FILENAME = "api.json"


DEFAULT_PORT = 8765


#: Loopback only, and not configurable. See docs/API.md: the token is readable
#: by anything running as this user, so it is not a credential that would make
#: exposing the port to a network safe.
BIND_HOST = "127.0.0.1"


#: How long a request that has to reach the cluster (a log tail, a directory
#: listing) may take before the client is told so, rather than hanging.
#: A fetch of files named by path can be large, and the reply is the list of
#: what landed -- so it is held longer than a listing.
REMOTE_TIMEOUT = 120.0


DOWNLOAD_TIMEOUT = 1800.0


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
    "submit_options": str,
    "fetch_globs": list,
    "auto_download": bool,
}


#: Every route, as ``/ping`` and an unknown path list them: a client that
#: never saw docs/API.md -- a script, an agent -- otherwise had to guess.
#: Kept in step with the dispatch in :meth:`JobApi.handle` by a test.
ROUTES = (
    ("GET", "/ping", "version, and how many jobs and hosts there are"),
    ("GET", "/hosts", "configured hosts"),
    ("GET", "/hosts/{id}/status", "load, memory, cores, and the helper queue; ?stats=0"),
    ("GET", "/hosts/{id}/files", "list any directory on the host; ?path=, ?depth="),
    ("GET", "/hosts/{id}/file", "is a path there, its size and sha256; ?path=, ?hash=0"),
    ("POST", "/hosts/{id}/download", "fetch files by path from anywhere on the host"),
    ("GET", "/presets", "saved presets; ?host="),
    ("GET", "/jobs", "tracked jobs; ?state=, ?host=, ?name=, ?limit="),
    ("POST", "/jobs", "submit a job: host plus command or preset, files"),
    ("GET", "/jobs/{id}", "one job"),
    ("DELETE", "/jobs/{id}", "stop tracking a finished job"),
    ("POST", "/jobs/{id}/cancel", "cancel it on the host"),
    ("POST", "/jobs/{id}/download", "fetch its results"),
    ("GET", "/jobs/{id}/log", "tail its log; ?lines=, ?file="),
    ("GET", "/jobs/{id}/files", "list its remote directory"),
    ("POST", "/jobs/{id}/force", "start it now, ahead of the helper queue"),
    ("POST", "/jobs/{id}/recheck", "look again at a LOST job"),
)


class ApiError(Exception):
    """A request that cannot be served, carrying the status the client gets."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)

    def payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"error": self.message, "status": self.status}
        if self.status == 404 and self.message.startswith("Unknown "):
            payload["routes"] = route_list()
        return payload


class Deferred:
    """A reply that is not ready when the handler returns.

    A log tail has to reach the host, and the handler runs on the GUI thread --
    which is also the thread the answer arrives on, so waiting there would
    deadlock. The handler returns one of these instead and the socket thread,
    which has nothing else to do, waits on it.
    """

    def __init__(self, timeout: float = REMOTE_TIMEOUT) -> None:
        #: How long the socket thread waits for this one.
        self.timeout = float(timeout)
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


def route_list() -> List[str]:
    return [f"{method} {API_PREFIX}{path} - {what}" for method, path, what in ROUTES]
