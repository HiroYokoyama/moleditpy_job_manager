"""SSH terminal argument safety and detached native-terminal launch behavior."""

import base64
import os
import shlex
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from job_manager import ssh_terminal as terminal
from job_manager.models import BACKEND_LOCAL, SCHEDULER_WINDOWS, HostProfile


def host(**overrides):
    values = dict(id="h1", name="myhost", hostname="myhost", username="user")
    values.update(overrides)
    return HostProfile(**values)


def test_home_session_has_real_pty_and_no_remote_command():
    argv = terminal.ssh_arguments(host())
    assert argv[-3:] == ["-tt", "--", "user@myhost"]
    assert "BatchMode=no" in argv
    assert "RemoteCommand=none" in argv


def test_connection_options_preserve_key_jump_port_and_custom_options():
    argv = terminal.ssh_arguments(
        host(
            port=2222,
            key_path="/keys/my key",
            jump_host="gateway",
            ssh_options=["ServerAliveInterval=30"],
        )
    )
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-i") + 1] == "/keys/my key"
    assert argv[argv.index("-J") + 1] == "gateway"
    assert "ServerAliveInterval=30" in argv


def test_default_port_preserves_ssh_config_alias_port():
    assert "-p" not in terminal.ssh_arguments(host())


@pytest.mark.parametrize(
    "directory",
    [
        "/tmp/job space",
        "/tmp/job'; touch injected; echo '",
        "~/jobs/current",
        "/tmp/$(touch injected)",
    ],
)
def test_job_directory_is_a_single_quoted_shell_argument(directory):
    command = terminal.ssh_arguments(host(), directory)[-1]
    assert command.startswith("cd -- ")
    tokens = shlex.split(command)
    assert tokens[2] == directory
    assert tokens[3:] == ["&&", "exec", "${SHELL:-/bin/sh}", "-l"]


def test_windows_remote_directory_uses_interactive_encoded_powershell():
    command = terminal.ssh_arguments(host(scheduler=SCHEDULER_WINDOWS), "C:/jobs/user's result")[-1]
    assert "-NoExit" in command
    assert "-NonInteractive" not in command
    source = base64.b64decode(command.split()[-1]).decode("utf-16-le")
    assert "user''s result" in source
    assert "-ErrorAction Stop" in source


def test_local_backend_is_not_an_ssh_host():
    assert not terminal.supports_ssh(host(backend=BACKEND_LOCAL))
    with pytest.raises(ValueError):
        terminal.ssh_arguments(host(backend=BACKEND_LOCAL))


@pytest.fixture
def launcher(monkeypatch):
    process = MagicMock()
    monkeypatch.setattr(terminal.subprocess, "Popen", process)
    monkeypatch.setattr(terminal, "_launchers", [])
    return process


def test_windows_launch_allocates_console_without_redirecting_pipes(monkeypatch, launcher):
    monkeypatch.setattr(terminal.sys, "platform", "win32")
    monkeypatch.setattr(terminal.shutil, "which", lambda name: "ssh.exe")
    terminal.launch_ssh_terminal(host())
    args, kwargs = launcher.call_args
    assert args[0][0] == "ssh.exe"
    assert kwargs == {"creationflags": 0x10}


def test_linux_launch_passes_argv_without_local_shell(monkeypatch, launcher):
    monkeypatch.setattr(terminal.sys, "platform", "linux")
    monkeypatch.setattr(
        terminal.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("ssh", "gnome-terminal") else None,
    )
    terminal.launch_ssh_terminal(host())
    args, kwargs = launcher.call_args
    assert args[0][:3] == ["/usr/bin/gnome-terminal", "--", "/usr/bin/ssh"]
    assert kwargs == {"start_new_session": True}


def test_mac_launch_uses_private_self_removing_script(monkeypatch, launcher, tmp_path):
    monkeypatch.setattr(terminal.sys, "platform", "darwin")
    monkeypatch.setattr(terminal.shutil, "which", lambda name: "/usr/bin/ssh")
    original = terminal.tempfile.mkstemp
    monkeypatch.setattr(
        terminal.tempfile, "mkstemp", lambda **kwargs: original(dir=tmp_path, **kwargs)
    )
    terminal.launch_ssh_terminal(host())
    args, _ = launcher.call_args
    script = Path(args[0][-1])
    assert args[0][:3] == ["open", "-a", "Terminal"]
    assert 'rm -f -- "$0"' in script.read_text(encoding="utf-8")
    assert "exec /usr/bin/ssh" in script.read_text(encoding="utf-8")
    if os.name != "nt":
        assert script.stat().st_mode & 0o777 == 0o700


def test_missing_ssh_reports_error_without_starting_process(monkeypatch, launcher):
    monkeypatch.setattr(terminal.shutil, "which", lambda name: None)
    with pytest.raises(OSError, match="OpenSSH"):
        terminal.launch_ssh_terminal(host())
    launcher.assert_not_called()


def test_missing_linux_terminal_reports_error(monkeypatch, launcher):
    monkeypatch.setattr(terminal.sys, "platform", "linux")
    monkeypatch.setattr(
        terminal.shutil, "which", lambda name: "/usr/bin/ssh" if name == "ssh" else None
    )
    with pytest.raises(OSError, match="terminal"):
        terminal.launch_ssh_terminal(host())
    launcher.assert_not_called()
