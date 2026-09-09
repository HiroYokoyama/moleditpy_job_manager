"""The loopback HTTP server that puts :mod:`job_manager.api_core` on a socket.

Two things this has to get right, and neither is about HTTP:

* **The store has one writer thread.** Every handler runs on the GUI thread,
  reached through a queued signal; the socket thread waits for the answer. A
  request that touched the store from the socket thread would race the poller
  and the monitor, which is the class of bug that corrupts a job list.
* **A handler must not block the GUI thread.** Anything that has to reach the
  host answers with a :class:`~job_manager.api_core.Deferred`, which the socket
  thread waits on *after* the GUI thread has returned.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from PyQt6.QtCore import QObject, pyqtSignal

from .api_core import (
    API_PREFIX,
    BIND_HOST,
    DEFAULT_PORT,
    REMOTE_TIMEOUT,
    ApiError,
    Deferred,
    JobApi,
    ensure_token,
    remove_endpoint_file,
    tokens_match,
    write_endpoint_file,
)

#: Refused outright. A request body is a JSON document describing a job, not a
#: file upload -- the files themselves are named by path and read from disk.
MAX_BODY_BYTES = 1 << 20
#: How much of an over-sized body is read and thrown away so the client can
#: still be told why it was refused. Past this the connection is simply closed.
MAX_DRAIN_BYTES = 8 << 20

#: How long the socket thread waits for the GUI thread to run a handler. Long
#: enough to sit behind a modal dialog the user has open, short enough that a
#: wedged GUI answers the client instead of hanging it.
GUI_TIMEOUT = 30.0

_AUTH_HEADER = "Authorization"
_TOKEN_HEADER = "X-Job-Manager-Token"


class _GuiBridge(QObject):
    """Runs a callable on the thread this object lives on.

    Constructed on the GUI thread, so the queued connection delivers there;
    ``call`` is invoked from a socket thread and blocks until the answer is in.
    """

    _requested = pyqtSignal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._requested.connect(self._run)

    @staticmethod
    def _run(box: Dict[str, Any]) -> None:
        try:
            box["result"] = box["fn"]()
        except BaseException as exc:  # noqa: BLE001 - carried to the caller verbatim
            box["error"] = exc
        finally:
            box["done"].set()

    def call(self, fn: Callable[[], Any], timeout: float = GUI_TIMEOUT) -> Any:
        box: Dict[str, Any] = {"fn": fn, "done": threading.Event()}
        self._requested.emit(box)
        if not box["done"].wait(timeout):
            raise ApiError(503, "MoleditPy is busy and did not handle the request in time")
        if "error" in box:
            raise box["error"]
        return box.get("result")


class _Handler(BaseHTTPRequestHandler):
    #: Whether anything has taken responsibility for the request body yet.
    #: A refusal decided before it is read has to drain it first.
    _body_seen = False
    """One request. ``server.api_hook`` does everything that is not HTTP."""

    server_version = "MoleditPyJobManager"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._serve("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default writes to stderr, which is MoleditPy's console.
        logging.debug("Job Manager API: " + fmt, *args)

    # --- the request --------------------------------------------------------

    def _serve(self, method: str) -> None:
        self._body_seen = False
        try:
            status, payload = self._dispatch(method)
        except ApiError as exc:
            status, payload = exc.status, exc.payload()
        except Exception as exc:  # noqa: BLE001 - never take the server down
            logging.exception("Job Manager API: request failed")
            status, payload = 500, {"error": str(exc), "status": 500}
        if status >= 400:
            # The same reasoning the 413 path already had, applied to every
            # refusal: origin and token are checked before the body is read,
            # so answering here would close the socket while the client is
            # still writing. The client then sees a connection reset instead
            # of the 401 telling it the token was wrong -- and on Windows that
            # is a ConnectionAbortedError, which is how this first showed up.
            self._drain_unread()
        self._reply(status, payload)

    def _drain_unread(self) -> None:
        """Consume a body nothing has read yet, so the reply can be delivered."""
        if self._body_seen:
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return
        if length > 0:
            self._drain(length)

    def _dispatch(self, method: str) -> Tuple[int, Any]:
        hook = getattr(self.server, "api_hook", None)
        if hook is None:  # pragma: no cover - the server always sets one
            raise ApiError(503, "The API is not running")
        self._check_origin()
        self._check_token(hook.token())
        split = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(split.query).items()}
        body = self._body()
        status, payload = hook.call(method, split.path, query, body)
        if isinstance(payload, Deferred):
            # Waited for here, off the GUI thread, so a slow cluster delays one
            # client rather than freezing MoleditPy.
            payload = payload.wait(REMOTE_TIMEOUT)
        return status, payload

    def _check_origin(self) -> None:
        """Refuse a request a web page made on the user's behalf.

        A page on any site can POST to 127.0.0.1 from the user's browser. It
        cannot read the reply without CORS (which is never sent here) and it
        cannot read the token -- but a blind POST that submits a job would
        still be a job submitted. Browsers attach ``Origin`` to exactly those
        requests, so refusing one that carries a non-loopback origin closes it.
        """
        origin = self.headers.get("Origin", "")
        if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ApiError(403, "Cross-origin requests are refused")

    def _check_token(self, expected: str) -> None:
        presented = self.headers.get(_TOKEN_HEADER, "")
        if not presented:
            header = self.headers.get(_AUTH_HEADER, "")
            if header.lower().startswith("bearer "):
                presented = header[7:].strip()
        if not tokens_match(presented, expected):
            raise ApiError(
                401,
                f"A token is required. Send it as '{_AUTH_HEADER}: Bearer <token>'; "
                f"the token is in the api.json beside the job list.",
            )

    def _body(self) -> Dict[str, Any]:
        # Whatever happens below, this method owns the body from here on.
        self._body_seen = True
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ApiError(400, "Content-Length is not a number") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            # Drained first, and only then refused. Replying while the client
            # is still writing resets the connection, and the client sees a
            # dropped socket instead of the 413 explaining what it did wrong.
            self._drain(length)
            raise ApiError(413, f"The request body may not exceed {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(400, f"The request body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "The request body must be a JSON object")
        return data

    def _drain(self, length: int) -> None:
        """Read and discard a body being refused, without ever holding it.

        Bounded: past :data:`MAX_DRAIN_BYTES` the connection is closed instead,
        so nothing can keep this thread reading indefinitely.
        """
        remaining = min(int(length), MAX_DRAIN_BYTES)
        if remaining < length:
            self.close_connection = True
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _reply(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
        # The whole write, not only the body: a client that timed out and hung
        # up is gone by the header too, and on Windows that surfaces as
        # ConnectionAbortedError (WinError 10053) rather than a broken pipe --
        # which printed a traceback into MoleditPy's console for what is an
        # ordinary way for a request to end.
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            # Nothing here is for a browser to read, and saying so stops one
            # caching a job list that changes every few seconds.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            self.close_connection = True
            logging.debug("Job Manager API: the client went away before the reply")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    #: Off deliberately. The default rebinds a port another process is still
    #: listening on in some configurations, and quietly serving a second
    #: MoleditPy's clients is worse than refusing to start.
    allow_reuse_address = False
    api_hook: Any = None

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default prints a traceback to stderr, which is
        # MoleditPy's console. A client that disconnects mid-request is not
        # something the user can act on.
        logging.debug("Job Manager API: request from %s failed", client_address, exc_info=True)


class JobApiServer(QObject):
    """Owns the listening socket, the token and the endpoint file."""

    started = pyqtSignal(int)  # port
    stopped = pyqtSignal()

    def __init__(self, service: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.api = JobApi(service)
        self._bridge = _GuiBridge(self)
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._token = ""
        self._port = 0

    # --- lifetime -----------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._server is not None

    def url(self) -> str:
        return f"http://{BIND_HOST}:{self._port}{API_PREFIX}" if self._port else ""

    def token(self) -> str:
        return self._token

    def renew_token(self) -> str:
        """Issue a new secret, invalidating every client using the old one."""
        self._token = ensure_token(self.service.store.directory, renew=True)
        if self.running:
            write_endpoint_file(self.service.store.directory, self._port, self._token)
        return self._token

    def start(self, port: int = DEFAULT_PORT) -> int:
        """Listen on ``port``; returns the port actually bound.

        Port 0, or a port already taken, falls back to one the operating system
        picks -- the endpoint file names it, so a client that discovers the API
        rather than being told a number is unaffected either way.
        """
        if self.running:
            return self._port
        self._token = ensure_token(self.service.store.directory)
        server = self._bind(int(port or 0))
        server.api_hook = self
        self._server = server
        self._port = server.server_address[1]
        self._thread = threading.Thread(
            # Not the 0.5 s default: this interval is how long stop() waits,
            # and a plugin reload should not sit on it half a second.
            target=lambda: server.serve_forever(poll_interval=0.05),
            name="job-manager-api",
            daemon=True,
        )
        self._thread.start()
        write_endpoint_file(self.service.store.directory, self._port, self._token)
        logging.info("Job Manager: local API listening on %s", self.url())
        self.started.emit(self._port)
        return self._port

    @staticmethod
    def _bind(port: int) -> _Server:
        try:
            return _Server((BIND_HOST, port), _Handler)
        except OSError as exc:
            if not port:
                raise
            logging.warning(
                "Job Manager: port %s is not available (%s); taking a free one instead",
                port,
                exc,
            )
            try:
                return _Server((BIND_HOST, 0), _Handler)
            except OSError as fallback:
                raise OSError(f"Could not listen on {BIND_HOST}: {fallback}") from fallback

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self._port = 0
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            logging.debug("Job Manager: the API socket did not close cleanly", exc_info=True)
        if thread is not None:
            thread.join(timeout=5)
        remove_endpoint_file(self.service.store.directory)
        logging.info("Job Manager: local API stopped")
        self.stopped.emit()

    # --- what the handler calls ---------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        body: Dict[str, Any],
    ) -> Tuple[int, Any]:
        return self._bridge.call(lambda: self.api.handle(method, path, query, body))


def port_is_free(port: int) -> bool:
    """Whether the API could bind ``port`` right now. For the settings dialog."""
    if not port:
        return True
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((BIND_HOST, int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


__all__ = ["GUI_TIMEOUT", "MAX_BODY_BYTES", "JobApiServer", "port_is_free"]
