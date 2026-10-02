"""Hand job tracking to a process of its own when MoleditPy closes.

The plugin lives inside MoleditPy's process, so keeping it alive used to mean
keeping all of MoleditPy alive with its window hidden -- the 3D view and every
loaded molecule still in memory for a queue that may run all night. Instead,
on the way out, the plugin starts ``python job_manager/__main__.py --tray``:
the same package, run on its own, polling from the tray. MoleditPy then quits
for real.

The two must never both track: two pollers query every host twice and every
job ending is announced twice. The tray process registers itself in the
instance registry (see :mod:`job_manager.instances`); a MoleditPy starting up
asks it to stop there and waits for its heartbeat to go before reading the job
list, and a standalone launch asks it to bring its monitor up instead of
starting beside it.

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

#: The heartbeat and stop files of a 2.0 / 2.1 tray process. Newer ones are
#: in the instance registry (see instances.py); these remain only so a
#: MoleditPy updated while one of those runs can still stop it.
TRAY_FILE = "tray.json"
STOP_FILE = "tray.stop"

#: A heartbeat older than this is a process that crashed or was killed.
STALE_AFTER_SECONDS = 10.0


def tray_path(directory: str) -> str:
    return os.path.join(directory, TRAY_FILE)


def stop_path(directory: str) -> str:
    return os.path.join(directory, STOP_FILE)


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


def standalone_command(
    package_dir: str, relaunch: Optional[List[str]] = None, after_pid: int = 0
) -> List[str]:
    """The command line for the tray process.

    ``__main__.py`` by path rather than ``-m``: the plugin folder's name is
    whatever the installer gave it, and need not be importable as a module name.
    ``after_pid`` is the process handing over, which is still running -- and
    still registered -- while the tray process starts.
    """
    command = [background_python(), os.path.join(package_dir, "__main__.py"), "--tray"]
    if relaunch:
        command += ["--relaunch", json.dumps(relaunch)]
    if after_pid:
        command += ["--after-pid", str(int(after_pid))]
    return command


def _launched_package(main_module: Any, modules: Dict[str, Any]) -> str:
    """The package a console-script launcher started, if it has a ``__main__``.

    pip's launcher script does ``from moleditpy.__main__ import main``, so
    that module is already imported and ``main`` says which package it is.
    """
    function = getattr(main_module, "main", None)
    top = (getattr(function, "__module__", "") or "").split(".")[0]
    if top and top != "__main__" and f"{top}.__main__" in modules:
        return top
    return ""


def relaunch_command(
    argv: Optional[List[str]] = None,
    executable: Optional[str] = None,
    main_module: Any = None,
    modules: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """How to start MoleditPy again, read off the process that is running it.

    Empty when there is no reliable answer: the tray menu then offers no
    "Open MoleditPy" at all, rather than one that silently starts nothing.
    """
    if argv is None:
        main_module = sys.modules.get("__main__") if main_module is None else main_module
        modules = sys.modules if modules is None else modules
    argv = list(sys.argv if argv is None else argv)
    executable = executable or sys.executable
    if getattr(sys, "frozen", False):
        return [executable]
    script = argv[0] if argv else ""
    if not script or script == "-c":
        return []
    if os.path.dirname(os.path.abspath(script)) == os.path.dirname(os.path.abspath(__file__)):
        # This package run on its own: there is no MoleditPy to go back to,
        # and "Open MoleditPy" must not open another standalone monitor.
        return []
    if os.path.basename(script) == "__main__.py":
        # `python -m moleditpy`: running __main__.py by path would break its
        # relative imports.
        return [executable, "-m", os.path.basename(os.path.dirname(script))]
    # pip's launcher on Windows strips ".exe" from argv[0], so the script
    # named there does not exist: `python Scripts\moleditpy` started nothing.
    launcher = script if script.lower().endswith(".exe") else script + ".exe"
    if script == launcher or (not os.path.isfile(script) and os.path.isfile(launcher)):
        package = _launched_package(main_module, modules or {})
        if package:
            # The interpreter, not the launcher: a console launcher started
            # detached opens a console window of its own for the Python it runs.
            return [executable, "-m", package]
        return [launcher]
    # Absolute: the tray process starts it from another working directory.
    script = os.path.abspath(script)
    if not os.path.isfile(script):
        return []
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
    "STALE_AFTER_SECONDS",
    "background_python",
    "can_hand_off",
    "clear_stop",
    "live_tray",
    "relaunch_command",
    "request_stop",
    "spawn_detached",
    "standalone_command",
    "stop_requested",
    "stop_running_tray",
]
