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

Reading the registry and then registering is not one step: a monitor registers
only after its window, its service and its tray icon exist, a second or more
after it looked. Two launches started together each found nobody and both
became trackers. :class:`SingleInstanceLock` closes that: an operating-system
lock the one standalone monitor or tray process holds from before it looks
until it exits, and that the system drops for a process that dies.
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

LOCK_NAME = "standalone.lock"
#: How long a launch waits on a lock holder that neither registers nor exits.
CLAIM_TIMEOUT_SECONDS = 20.0
#: A tray process may be waiting out the shutdown of the monitor that started it.
TRAY_CLAIM_TIMEOUT_SECONDS = 30.0
CLAIM_POLL_SECONDS = 0.2
#: How long a MoleditPy starting up gives a tray process that is still starting.
STARTING_WAIT_SECONDS = 3.0


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


def _lock_handle(handle) -> None:
    """Take the lock or raise OSError. Never waits."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SingleInstanceLock:
    """Held by the one standalone monitor or tray process, for as long as it lives.

    A lock on an open file rather than a file that exists: the system releases
    it when the process ends however it ends, so a crash leaves nothing behind
    for the next launch to mistake for a running tracker. Nothing is written to
    the file -- on Windows a locked byte cannot be read by anyone else.
    """

    def __init__(self, directory: str) -> None:
        self.path = os.path.join(instances_dir(directory), LOCK_NAME)
        self._handle = None
        #: The lock could not be tried at all (a read-only data directory):
        #: the launch goes ahead on the registry alone, as it did before.
        self.unavailable = False

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """True when this process now holds it, or when it cannot be tried."""
        if self._handle is not None:
            return True
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            # Not inherited: the tray process this one starts on its way out
            # must take the lock itself, not be born holding its parent's.
            handle = open(self.path, "a+b")  # noqa: SIM115 - kept open on purpose
        except OSError:
            logging.debug("Job Manager: no single-instance lock", exc_info=True)
            self.unavailable = True
            return True
        try:
            _lock_handle(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock_handle(handle)
        except OSError:
            logging.debug("Job Manager: single-instance lock not unlocked", exc_info=True)
        handle.close()


def standalone_lock_held(directory: str) -> bool:
    """Some other process holds the lock: a standalone or tray, running or starting."""
    lock = SingleInstanceLock(directory)
    if not os.path.exists(lock.path):
        return False
    if not lock.acquire():
        return True
    lock.release()
    return False


def _pid_running(pid: int) -> bool:
    """False only when no process with *pid* exists on this machine.

    Checked before the socket: on Windows a refused loopback connection takes
    0.5-2 s to report, so every registration a crashed MoleditPy left behind
    used to stall the next start by that much.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_INVALID_PARAMETER means no such process; anything else
            # (access denied) means it exists.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            # 259 is STILL_ACTIVE; a process that exited with 259 falls through
            # to the socket check, which is no worse than before.
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: it exists, under another user
    return True


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
            alive = _pid_running(pid) and _socket_alive(data)
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


def claim_or_defer(
    directory: str,
    action: str,
    timeout: float = CLAIM_TIMEOUT_SECONDS,
    poll: float = CLAIM_POLL_SECONDS,
) -> "tuple[Optional[SingleInstanceLock], Optional[Dict[str, Any]]]":
    """What a launch by hand does first: become the one tracker, or ask the one there.

    Returns ``(lock, None)`` when this launch should start -- holding the lock,
    so a launch a moment behind it waits instead of starting too -- and
    ``(None, instance)`` when a running Job Manager took ``action``.

    ``(None, None)`` is the holder that never registered and never let go: it
    is hung, and the launch starts beside it as it always has.
    """
    lock = SingleInstanceLock(directory)
    deadline = time.monotonic() + timeout
    while True:
        if lock.acquire():
            # No standalone is running or starting. A MoleditPy may be.
            target = defer_to_running(directory, action)
            if target is not None:
                lock.release()
                return None, target
            return lock, None
        # Held by another launch. It answers once it has registered.
        for data in live_instances(directory):
            if data.get("role") not in (ROLE_STANDALONE, ROLE_TRAY):
                continue
            pid = int(data["pid"])
            send_request(directory, pid, action)
            if wait_until_taken(directory, pid):
                return None, data
            withdraw_request(directory, pid)
            break
        if time.monotonic() >= deadline:
            return None, None
        time.sleep(poll)


def claim_for_tray(
    directory: str,
    after_pid: int = 0,
    timeout: float = TRAY_CLAIM_TIMEOUT_SECONDS,
    poll: float = CLAIM_POLL_SECONDS,
) -> Optional[SingleInstanceLock]:
    """The lock for a tray process, or None when another tracker makes it unnecessary.

    The process handing over may be a standalone monitor, which holds the lock
    until it has exited: that is waited out. Any other holder registers within
    moments, and this process then has nothing to do.
    """
    lock = SingleInstanceLock(directory)
    deadline = time.monotonic() + timeout
    while True:
        if standalone_running(directory, after_pid):
            return None
        if lock.acquire() or time.monotonic() >= deadline:
            return lock
        time.sleep(poll)


def wait_while_starting(
    directory: str, timeout: float = STARTING_WAIT_SECONDS, poll: float = 0.1
) -> None:
    """Give a standalone or tray process that holds the lock time to register.

    For a MoleditPy opened straight after another was closed: the tray process
    that one started is not in the registry yet, so it could not be asked to
    stop, and both would track.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not standalone_lock_held(directory):
            return
        if any(
            data.get("role") in (ROLE_STANDALONE, ROLE_TRAY) for data in live_instances(directory)
        ):
            return
        time.sleep(poll)


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
    "SingleInstanceLock",
    "claim_for_tray",
    "claim_or_defer",
    "defer_to_running",
    "live_instances",
    "pick_target",
    "register_instance",
    "request_pending",
    "send_request",
    "standalone_lock_held",
    "standalone_running",
    "take_request",
    "unregister_instance",
    "wait_until_gone",
    "wait_until_taken",
    "wait_while_starting",
    "withdraw_request",
]
