"""Which Job Managers are running, and asking one of them to show itself.

Every running Job Manager -- the plugin inside a MoleditPy, the standalone
monitor, the tray process MoleditPy hands its jobs to -- registers once in
``<data dir>/instances/``. Liveness is checked through a loopback socket,
without periodic disk writes. A standalone monitor reads the entries first: when
one is already running it asks that one to bring its window up and exits,
rather than starting a second tracker that would query every host twice and
announce every job ending twice. (Two MoleditPy windows each run the plugin,
and that is accepted -- it is the host that decides to start twice.)

The random socket token distinguishes reused ports and PIDs. Everything uses
the standard library; the socket only proves liveness and cannot execute actions.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional

INSTANCES_DIR = "instances"

ROLE_MOLEDITPY = "moleditpy"
ROLE_STANDALONE = "standalone"
ROLE_TRAY = "tray"

#: How often an instance looks for a request addressed to it.
REQUEST_POLL_SECONDS = 0.4
PROBE_TIMEOUT_SECONDS = 0.2
# Windows can take two seconds to report a refused loopback connection.
CONNECT_TIMEOUT_SECONDS = 2.5

ACTION_SHOW_MONITOR = "show_monitor"
ACTION_SHOW_HOST_MONITOR = "show_host_monitor"
ACTION_STOP = "stop"


class LivenessServer:
    """A process-local token responder; it never touches Qt or job state."""

    def __init__(self) -> None:
        self.token = secrets.token_hex(32)
        self._closed = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._socket.bind(("127.0.0.1", 0))
            self._socket.listen(16)
            self._socket.settimeout(0.2)
        except BaseException:
            self._socket.close()
            raise
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="job-instance", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._socket.close()
            raise

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    connection.settimeout(PROBE_TIMEOUT_SECONDS)
                    connection.sendall(self.token.encode("ascii"))
                except OSError:
                    pass

    def close(self) -> None:
        self._closed.set()
        self._socket.close()
        self._thread.join(timeout=1.0)


def _socket_alive(data: Dict[str, Any]) -> bool:
    port = data.get("port")
    token = data.get("token")
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    if (
        not isinstance(token, str)
        or len(token) != 64
        or any(c not in "0123456789abcdef" for c in token)
    ):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), CONNECT_TIMEOUT_SECONDS) as connection:
            deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
            received = b""
            while len(received) < 64:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True  # Uncertain: do not discard a busy live tracker.
                connection.settimeout(remaining)
                chunk = connection.recv(64 - len(received))
                if not chunk:
                    return False
                received += chunk
            return secrets.compare_digest(received, token.encode("ascii"))
    except socket.timeout:
        return True  # A slow response is not proof of death.
    except OSError:
        return False


def instances_dir(directory: str) -> str:
    return os.path.join(directory, INSTANCES_DIR)


def _registration_path(directory: str, pid: int) -> str:
    return os.path.join(instances_dir(directory), f"{int(pid)}.json")


def _request_path(directory: str, pid: int) -> str:
    return os.path.join(instances_dir(directory), f"{int(pid)}.request")


def _write_atomically(path: str, data: Dict[str, Any]) -> None:
    """Replace transient IPC metadata whole; it needs no durable disk flush."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp{os.getpid()}"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        # Replaced, not rewritten: a reader mid-write would see an empty file
        # and take a live instance for a dead one.
        try:
            os.replace(temp, path)
        except PermissionError:
            # Windows refuses to replace a file another process has open --
            # a launch reading this registration at that very moment. It lets go
            # within milliseconds.
            time.sleep(0.05)
            os.replace(temp, path)
    except OSError:
        # Never a temp file left behind for every refused write.
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def register_instance(directory: str, role: str, started: float, server: LivenessServer) -> None:
    """Publish discovery information once, after the responder is listening."""
    _write_atomically(
        _registration_path(directory, os.getpid()),
        {
            "pid": os.getpid(),
            "role": role,
            "started": started,
            "protocol": 2,
            "port": server.port,
            "token": server.token,
        },
    )


def unregister_instance(directory: str) -> None:
    for path in (_registration_path(directory, os.getpid()), _request_path(directory, os.getpid())):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logging.debug("Job Manager: %s not removed", path, exc_info=True)


def live_instances(directory: str) -> List[Dict[str, Any]]:
    """Every other running Job Manager, the most recently started first.

    A closed responder is a process that died without cleaning up; its
    entry is removed here so the directory does not fill with the dead.
    """
    folder = instances_dir(directory)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            pid = int(data["pid"])
            if name != f"{pid}.json":
                continue
            if pid == os.getpid():
                continue
            if data.get("protocol") != 2:
                continue
            alive = _socket_alive(data)
        except (OSError, ValueError, TypeError, KeyError):
            # Unreadable now may be a file a moment from being replaced; it is
            # skipped this time, not deleted.
            continue
        if not alive:
            for stale in (path, _request_path(directory, pid)):
                try:
                    os.remove(stale)
                except OSError:
                    pass
            continue
        found.append(data)
    found.sort(key=lambda data: float(data.get("started", 0) or 0), reverse=True)
    return found


def pick_target(instances: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The instance a standalone launch should defer to, if any.

    A standalone monitor or a tray process first -- each is the one Job Manager
    of its kind on this machine -- then the most recently started MoleditPy.
    """
    for role in (ROLE_STANDALONE, ROLE_TRAY, ROLE_MOLEDITPY):
        for data in instances:
            if data.get("role") == role:
                return data
    return None


def standalone_running(directory: str, ignore_pid: int = 0) -> bool:
    """A standalone monitor or tray process is running, other than ``ignore_pid``.

    What a new tray process asks before starting: one of those already tracks.
    A MoleditPy is not counted -- two of those each run the plugin, and that
    is accepted -- nor is the process handing over, still registered as the
    tray process starts.
    """
    for data in live_instances(directory):
        if int(data.get("pid", 0) or 0) == int(ignore_pid or 0):
            continue
        if data.get("role") in (ROLE_STANDALONE, ROLE_TRAY):
            return True
    return False


def send_request(directory: str, pid: int, action: str) -> None:
    """Ask instance ``pid`` to do ``action`` at its next look."""
    _allow_foreground(pid)
    _write_atomically(
        _request_path(directory, pid), {"action": action, "from": os.getpid(), "at": time.time()}
    )


def request_pending(directory: str, pid: int) -> bool:
    return os.path.exists(_request_path(directory, pid))


def withdraw_request(directory: str, pid: int) -> None:
    try:
        os.remove(_request_path(directory, pid))
    except OSError:
        pass


def take_request(directory: str) -> Optional[str]:
    """The action addressed to this process, if one is waiting. Consumed."""
    path = _request_path(directory, os.getpid())
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        data = {}
    try:
        os.remove(path)
    except OSError:
        pass
    action = data.get("action") if isinstance(data, dict) else None
    return str(action) if action else None


def wait_until_taken(directory: str, pid: int, timeout: float = 5.0, poll: float = 0.1) -> bool:
    """True once ``pid`` has read its request; False if it never did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not request_pending(directory, pid):
            return True
        time.sleep(poll)
    return not request_pending(directory, pid)


def wait_until_gone(directory: str, pid: int, timeout: float = 6.0, poll: float = 0.1) -> bool:
    """True once ``pid`` no longer has a live registration."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(int(data["pid"]) != int(pid) for data in live_instances(directory)):
            return True
        time.sleep(poll)
    return False


def defer_to_running(directory: str, action: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """Hand ``action`` to the Job Manager already running, if there is one.

    Returns the instance that took it, or None when there is none to ask -- or
    when the one there never read the request (hung, or exiting), in which case
    the request is withdrawn and the caller starts as if alone.
    """
    target = pick_target(live_instances(directory))
    if target is None:
        return None
    pid = int(target["pid"])
    send_request(directory, pid, action)
    if wait_until_taken(directory, pid, timeout=timeout):
        return target
    withdraw_request(directory, pid)
    return None


def _allow_foreground(pid: int) -> None:
    """Let ``pid`` bring its window to the front when it answers us.

    Windows refuses SetForegroundWindow to a process that is not in the
    foreground; the one that is -- this launch, which the user just started --
    can pass that right on. Without it the existing window only flashes in the
    task bar.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
    except Exception:
        logging.debug("Job Manager: foreground not granted", exc_info=True)


__all__ = [
    "ACTION_SHOW_HOST_MONITOR",
    "ACTION_SHOW_MONITOR",
    "ACTION_STOP",
    "LivenessServer",
    "REQUEST_POLL_SECONDS",
    "ROLE_MOLEDITPY",
    "ROLE_STANDALONE",
    "ROLE_TRAY",
    "defer_to_running",
    "live_instances",
    "pick_target",
    "register_instance",
    "request_pending",
    "send_request",
    "standalone_running",
    "take_request",
    "unregister_instance",
    "wait_until_gone",
    "wait_until_taken",
    "withdraw_request",
]
