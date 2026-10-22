"""Interactive OpenSSH sessions in the system terminal; no shared Qt workers."""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from .models import BACKEND_OPENSSH, BACKEND_PARAMIKO, SCHEDULER_WINDOWS

_launchers = []


def supports_ssh(host) -> bool:
    return bool(host and host.backend in (BACKEND_OPENSSH, BACKEND_PARAMIKO) and host.hostname)


def ssh_arguments(host, directory: str = "") -> list[str]:
    if not supports_ssh(host):
        raise ValueError("Choose an SSH host with a hostname configured.")
    argv = [
        "ssh",
        "-o",
        "BatchMode=no",
        "-o",
        "RemoteCommand=none",
        "-o",
        f"ConnectTimeout={int(host.connect_timeout or 10)}",
    ]
    if host.port:
        argv += ["-p", str(int(host.port))]
    if host.key_path:
        argv += ["-i", os.path.expanduser(host.key_path), "-o", "IdentitiesOnly=yes"]
    if host.jump_host:
        argv += ["-J", host.jump_host]
    for option in host.ssh_options or []:
        if option.strip():
            argv += ["-o", option.strip()]
    argv += ["-tt", "--", host.target]
    if directory:
        from . import dialect, remote_paths

        if host.scheduler == SCHEDULER_WINDOWS:
            command = (
                f"Set-Location -LiteralPath {dialect.POWERSHELL.quote(directory)} -ErrorAction Stop"
            )
            encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
            argv.append(f"powershell -NoLogo -NoExit -EncodedCommand {encoded}")
        else:
            argv.append(f'cd -- {remote_paths.quote(directory)} && exec "${{SHELL:-/bin/sh}}" -l')
    return argv


def launch_ssh_terminal(host, directory: str = "") -> None:
    argv = ssh_arguments(host, directory)
    ssh = shutil.which("ssh")
    if not ssh:
        raise OSError("OpenSSH (ssh) was not found. Install or enable the system OpenSSH client.")
    argv[0] = ssh
    kwargs = {}
    script = None
    if sys.platform == "win32":
        # A real console owns the PTY and authentication prompts; no redirected pipes.
        command = argv
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
    elif sys.platform == "darwin":
        fd, script = tempfile.mkstemp(prefix="moleditpy_ssh_", suffix=".command")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nrm -f -- "$0"\nexec ' + shlex.join(argv) + "\n")
        os.chmod(script, 0o700)
        command = ["open", "-a", "Terminal", script]
        kwargs["start_new_session"] = True
    else:
        for name, separator in (
            ("x-terminal-emulator", "-e"),
            ("gnome-terminal", "--"),
            ("konsole", "-e"),
            ("xterm", "-e"),
        ):
            terminal = shutil.which(name)
            if terminal:
                command = [terminal, separator, *argv]
                break
        else:
            raise OSError(
                "No supported system terminal found (x-terminal-emulator, gnome-terminal, konsole, xterm)."
            )
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError:
        if script:
            os.unlink(script)
        raise
    # Retain only live launchers. The terminal remains independent of plugin shutdown.
    _launchers[:] = [item for item in _launchers if item.poll() is None]
    _launchers.append(process)
