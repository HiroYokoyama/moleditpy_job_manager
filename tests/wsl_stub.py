"""A stand-in for ``wsl.exe`` that runs commands through a local POSIX shell.

WSL cannot be installed on a CI runner, and a developer's machine may have only
a container distribution with no bash in it -- so the WSL backend would be the
one submission path with no end-to-end test at all. This accepts exactly the
command line :class:`~job_manager.transport.wsl.WSLTransport` builds::

    wsl.exe [-d <distro>] --cd / -- <shell> -lc <command>

and runs ``<command>`` through the real bash this machine has, translating the
one WSL-specific call the transport makes (``wslpath``) into its Git Bash
equivalent (``cygpath``). Everything else -- the argv shape, the quoting of a
Windows path inside the command, the copies that carry a file across -- is
exercised for real.
"""

from __future__ import annotations

import os
import subprocess
import sys


def main(argv: list) -> int:
    args = list(argv)
    distro = ""
    if args[:1] == ["-d"]:
        distro = args[1]
        args = args[2:]
    if args[:2] == ["--cd", "/"]:
        args = args[2:]
    if args[:1] != ["--"]:
        sys.stderr.write("wsl_stub: unexpected command line\n")
        return 2
    args = args[1:]
    if not distro:
        # What wsl.exe says with nothing installed, so the transport's own
        # handling of that message is covered too.
        if os.environ.get("WSL_STUB_NO_DISTRO"):
            sys.stderr.write("Windows Subsystem for Linux has no installed distributions.\n")
            return 1
    shell, flag, command = args[0], args[1], args[2]
    if shell != os.environ.get("WSL_STUB_SHELL", "bash"):
        sys.stderr.write(f"{shell}: not found\n")
        return 127
    bash = os.environ["WSL_STUB_BASH"]
    # The one call that is WSL's own. cygpath answers the same question for the
    # bash that ships with Git for Windows.
    command = command.replace("wslpath -a -u ", "cygpath -a -u ")
    proc = subprocess.run([bash, flag, command], capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
