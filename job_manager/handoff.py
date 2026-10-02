"""Hand job tracking to a process of its own when MoleditPy closes.

The plugin lives inside MoleditPy's process, so keeping it alive used to mean
keeping all of MoleditPy alive with its window hidden -- the 3D view and every
loaded molecule still in memory for a queue that may run all night. Instead,
on the way out, the plugin starts ``python job_manager/__main__.py --tray``:
the same package, run on its own, polling from the tray. MoleditPy then quits
for real.

The two must never both track: two pollers query every host twice and every
job ending is announced twice. The tray process writes a heartbeat file every
couple of seconds; a MoleditPy starting up asks it to stop, through a file
beside it, and waits for the heartbeat to go before reading the job list.
Files rather than a socket or a pid: the state directory is already shared by
every instance, and a heartbeat that stops being refreshed cannot be mistaken
for a live process the way a recycled pid can.

Pure stdlib, so the pytest-only CI job covers it.
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

TRAY_FILE = "tray.json"
STOP_FILE = "tray.stop"

#: How often the tray process proves it is alive.
HEARTBEAT_SECONDS = 2.0
#: A heartbeat older than this is a process that crashed or was killed.
STALE_AFTER_SECONDS = 10.0


def tray_path(directory: str) -> str:
    return os.path.join(directory, TRAY_FILE)


def stop_path(directory: str) -> str:
    return os.path.join(directory, STOP_FILE)


def write_heartbeat(directory: str, relaunch: Optional[List[str]] = None) -> None:
    """Say this process is the one tracking, and how to start MoleditPy again."""
    os.makedirs(directory, exist_ok=True)
    path = tray_path(directory)
    temp = f"{path}.tmp{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "beat": time.time(), "relaunch": relaunch or []}, handle)
    # Replaced, not rewritten: a MoleditPy reading mid-write would see an empty
    # file and take a live tray process for a dead one.
    os.replace(temp, path)


def live_tray(directory: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The running tray process's heartbeat, or None if there is none."""
    try:
        with open(tray_path(directory), encoding="utf-8") as handle:
            data = json.load(handle)
        beat = float(data.get("beat", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    current = time.time() if now is None else now
    if current - beat > STALE_AFTER_SECONDS:
        return None
    return data


def remove_tray_file(directory: str) -> None:
    for path in (tray_path(directory), stop_path(directory)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logging.debug("Job Manager: %s not removed", path, exc_info=True)


def request_stop(directory: str) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(stop_path(directory), "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def stop_requested(directory: str) -> bool:
    return os.path.exists(stop_path(directory))


def clear_stop(directory: str) -> None:
    try:
        os.remove(stop_path(directory))
    except FileNotFoundError:
        pass
    except OSError:
        logging.debug("Job Manager: the stop request was not cleared", exc_info=True)


def stop_running_tray(directory: str, timeout: float = 6.0, poll: float = 0.1) -> bool:
    """Ask a tray process to stop and wait for it. True if none is left.

    Waited for rather than fired and forgotten: the tray process may be part
    way through saving the job list, and the caller is about to read it.
    """
    if live_tray(directory) is None:
        return True
    request_stop(directory)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if live_tray(directory) is None:
            clear_stop(directory)
            return True
        time.sleep(poll)
    logging.warning("Job Manager: the background tray process did not stop in time")
    return False


def can_hand_off() -> bool:
    """Whether a separate Python can run this package at all.

    A frozen MoleditPy (PyInstaller and the like) has no interpreter to start:
    its ``sys.executable`` is MoleditPy itself.
    """
    return not getattr(sys, "frozen", False) and bool(sys.executable)


def background_python(executable: Optional[str] = None, platform: Optional[str] = None) -> str:
    """``pythonw.exe`` beside ``python.exe`` on Windows, so no console opens."""
    executable = executable or sys.executable
    platform = platform or sys.platform
    if platform == "win32":
        # ntpath, not os.path: the same answer whichever platform asks.
        folder, name = ntpath.split(executable)
        if name.lower() == "python.exe":
            candidate = ntpath.join(folder, "pythonw.exe")
            if os.path.exists(candidate):
                return candidate
    return executable


def standalone_command(package_dir: str, relaunch: Optional[List[str]] = None) -> List[str]:
    """The command line for the tray process.

    ``__main__.py`` by path rather than ``-m``: the plugin folder's name is
    whatever the installer gave it, and need not be importable as a module name.
    """
    command = [background_python(), os.path.join(package_dir, "__main__.py"), "--tray"]
    if relaunch:
        command += ["--relaunch", json.dumps(relaunch)]
    return command


def relaunch_command(
    argv: Optional[List[str]] = None, executable: Optional[str] = None
) -> List[str]:
    """How to start MoleditPy again, read off the process that is running it."""
    argv = list(sys.argv if argv is None else argv)
    executable = executable or sys.executable
    if getattr(sys, "frozen", False):
        return [executable]
    script = argv[0] if argv else ""
    if not script or script == "-c":
        return []
    if script.lower().endswith(".exe"):
        # A console-script launcher (moleditpy.exe in Scripts\\).
        return [script]
    if os.path.basename(script) == "__main__.py":
        # `python -m moleditpy`: running __main__.py by path would break its
        # relative imports.
        return [executable, "-m", os.path.basename(os.path.dirname(script))]
    return [executable, script]


def spawn_detached(command: List[str], cwd: Optional[str] = None) -> bool:
    """Start ``command`` so that it outlives this process. True if started."""
    if not command:
        return False
    kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
        )
    else:
        # Its own session: a terminal closing behind MoleditPy must not take
        # the tracker down with a SIGHUP.
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except (OSError, ValueError):
        logging.warning("Job Manager: could not start %s", command[0], exc_info=True)
        return False
    return True


__all__ = [
    "HEARTBEAT_SECONDS",
    "STALE_AFTER_SECONDS",
    "background_python",
    "can_hand_off",
    "clear_stop",
    "live_tray",
    "relaunch_command",
    "remove_tray_file",
    "request_stop",
    "spawn_detached",
    "standalone_command",
    "stop_requested",
    "stop_running_tray",
    "write_heartbeat",
]
