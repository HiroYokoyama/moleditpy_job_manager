"""Which Job Managers are running, and asking one of them to show itself.

Every running Job Manager -- the plugin inside a MoleditPy, the standalone
monitor, the tray process MoleditPy hands its jobs to -- keeps a small file in
``<data dir>/instances/`` saying it is alive and what it is, refreshed every
couple of seconds. A standalone monitor opened by hand reads them first: when
one is already running it asks that one to bring its window up and exits,
rather than starting a second tracker that would query every host twice and
announce every job ending twice. (Two MoleditPy windows each run the plugin,
and that is accepted -- it is the host that decides to start twice.)

Files, not a socket: the directory is already shared by every instance, a
heartbeat that stops being refreshed cannot be mistaken for a live process the
way a recycled pid can, and this module stays pure stdlib so the pytest-only CI
job covers it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

INSTANCES_DIR = "instances"

ROLE_MOLEDITPY = "moleditpy"
ROLE_STANDALONE = "standalone"
ROLE_TRAY = "tray"

#: How often an instance proves it is alive.
HEARTBEAT_SECONDS = 2.0
#: How often an instance looks for a request addressed to it.
REQUEST_POLL_SECONDS = 0.4
#: A heartbeat older than this is a process that crashed or was killed.
STALE_AFTER_SECONDS = 10.0

ACTION_SHOW_MONITOR = "show_monitor"
ACTION_SHOW_HOST_MONITOR = "show_host_monitor"
ACTION_STOP = "stop"


def instances_dir(directory: str) -> str:
    return os.path.join(directory, INSTANCES_DIR)


def _beat_path(directory: str, pid: int) -> str:
    return os.path.join(instances_dir(directory), f"{int(pid)}.json")


def _request_path(directory: str, pid: int) -> str:
    return os.path.join(instances_dir(directory), f"{int(pid)}.request")


def _write_atomically(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    # Replaced, not rewritten: a reader mid-write would see an empty file and
    # take a live instance for a dead one.
    os.replace(temp, path)


def write_heartbeat(directory: str, role: str, started: Optional[float] = None) -> None:
    """Say this process is a running Job Manager of the given role."""
    now = time.time()
    _write_atomically(
        _beat_path(directory, os.getpid()),
        {"pid": os.getpid(), "role": role, "beat": now, "started": started or now},
    )


def remove_heartbeat(directory: str) -> None:
    for path in (_beat_path(directory, os.getpid()), _request_path(directory, os.getpid())):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logging.debug("Job Manager: %s not removed", path, exc_info=True)


def live_instances(directory: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every other running Job Manager, the most recently started first.

    A heartbeat gone stale is a process that died without cleaning up; its
    files are removed here so the directory does not fill with the dead.
    """
    folder = instances_dir(directory)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    current = time.time() if now is None else now
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            pid = int(data["pid"])
            beat = float(data["beat"])
        except (OSError, ValueError, TypeError, KeyError):
            # Unreadable now may be a file a moment from being replaced; it is
            # skipped this time, not deleted.
            continue
        if pid == os.getpid():
            continue
        if current - beat > STALE_AFTER_SECONDS:
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
    """True once ``pid`` no longer has a live heartbeat."""
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
    "HEARTBEAT_SECONDS",
    "REQUEST_POLL_SECONDS",
    "ROLE_MOLEDITPY",
    "ROLE_STANDALONE",
    "ROLE_TRAY",
    "STALE_AFTER_SECONDS",
    "defer_to_running",
    "live_instances",
    "pick_target",
    "remove_heartbeat",
    "request_pending",
    "send_request",
    "take_request",
    "wait_until_gone",
    "wait_until_taken",
    "withdraw_request",
    "write_heartbeat",
]
