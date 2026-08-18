"""The two backends that reach a shell this plugin cannot assume: a WSL
distribution on this machine, and a Windows machine at the other end of an SSH
connection.

Both are about what is actually put on the wire. The end-to-end proof that a
job runs through WSL is in test_submission_paths.py; this is the shaping.
"""

from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from job_manager import dialect
from job_manager.models import (
    BACKEND_LOCAL,
    BACKEND_OPENSSH,
    BACKEND_WSL,
    SCHEDULER_SHELL,
    SCHEDULER_WINDOWS,
    HostProfile,
)
from job_manager.transport.base import CommandResult, TransportError
from job_manager.transport.wsl import WSLTransport, _quote


class TestAWindowsHostOverSSH(unittest.TestCase):
    """PowerShell has to survive whatever shell the SSH server hands it to."""

    def command_for(self, **overrides) -> str:
        fields = dict(name="winbox", hostname="win.example", backend=BACKEND_OPENSSH)
        fields.update(overrides)
        return dialect.wrap_remote(HostProfile(**fields), "Get-ChildItem -Path 'C:\\jobs'")

    def test_a_windows_host_is_sent_encoded_powershell(self):
        sent = self.command_for(scheduler=SCHEDULER_WINDOWS)
        self.assertIn("-EncodedCommand", sent)
        self.assertIn("powershell", sent)

    def test_the_encoding_round_trips(self):
        sent = self.command_for(scheduler=SCHEDULER_WINDOWS)
        encoded = sent.rsplit(" ", 1)[-1]
        self.assertEqual(
            base64.b64decode(encoded).decode("utf-16-le"), "Get-ChildItem -Path 'C:\\jobs'"
        )

    def test_nothing_a_shell_could_mangle_is_left_in_it(self):
        sent = self.command_for(scheduler=SCHEDULER_WINDOWS)
        for character in ("'", '"', "$", "|", "&", "\\"):
            self.assertNotIn(character, sent.rsplit(" ", 1)[-1])

    def test_a_posix_host_is_left_alone(self):
        self.assertNotIn("EncodedCommand", self.command_for(scheduler=SCHEDULER_SHELL))

    def test_the_local_windows_backend_is_left_alone(self):
        # PowerShell is started directly there; there is nothing in between.
        sent = self.command_for(scheduler=SCHEDULER_WINDOWS, backend=BACKEND_LOCAL)
        self.assertNotIn("EncodedCommand", sent)

    def test_the_transport_sends_the_wrapped_form(self):
        from job_manager.transport.openssh import OpenSSHTransport

        host = HostProfile(
            name="winbox",
            hostname="win.example",
            backend=BACKEND_OPENSSH,
            scheduler=SCHEDULER_WINDOWS,
            load_profile=False,
        )
        transport = OpenSSHTransport(host)
        with patch.object(
            OpenSSHTransport, "_spawn", return_value=CommandResult(0, "", "")
        ) as spawn:
            transport.run("'ok'")
        argv = spawn.call_args.args[0]
        self.assertIn("-EncodedCommand", " ".join(argv))


class TestTheWSLCommandLine(unittest.TestCase):
    def host(self, **overrides) -> HostProfile:
        fields = dict(
            name="wsl",
            backend=BACKEND_WSL,
            scheduler=SCHEDULER_SHELL,
            remote_root="/home/me/jobs",
            load_profile=False,
        )
        fields.update(overrides)
        return HostProfile(**fields)

    def transport(self, **overrides) -> WSLTransport:
        return WSLTransport(self.host(**overrides), exe="wsl.exe")

    def test_the_distribution_is_named_when_there_is_one(self):
        argv = self.transport(wsl_distro="Ubuntu")._argv("echo hi")
        self.assertEqual(argv[:3], ["wsl.exe", "-d", "Ubuntu"])

    def test_no_distribution_means_the_default_one(self):
        self.assertNotIn("-d", self.transport()._argv("echo hi"))

    def test_it_always_starts_from_a_directory_wsl_can_see(self):
        # Otherwise wsl.exe prints "Failed to translate <cwd>" into the output
        # being parsed, for any working directory it cannot map.
        argv = self.transport()._argv("echo hi")
        self.assertIn("--cd", argv)
        self.assertEqual(argv[argv.index("--cd") + 1], "/")

    def test_the_command_is_the_last_argument(self):
        argv = self.transport()._argv("echo hi")
        self.assertEqual(argv[-3:], ["bash", "-lc", "echo hi"])

    def test_a_host_that_reads_its_own_profile_needs_no_login_shell(self):
        # A login shell costs about 260 ms against 14 ms, on every command.
        argv = self.transport(load_profile=True)._argv("echo hi")
        self.assertIn("-c", argv)
        self.assertNotIn("-lc", argv)

    def test_a_windows_path_is_quoted_inside_the_command_never_beside_it(self):
        # wsl.exe eats the backslashes of an unquoted argument: C:\\Users
        # arrives as C:Users and wslpath rejects it.
        transport = self.transport()
        seen = {}

        def fake_run(cmd, timeout=None):
            seen["cmd"] = cmd
            return CommandResult(0, "/mnt/c/Users/me/mol.inp\n", "")

        with patch.object(WSLTransport, "run", side_effect=fake_run):
            translated = transport.to_wsl_path(r"C:\Users\me\mol.inp")

        self.assertEqual(translated, "/mnt/c/Users/me/mol.inp")
        self.assertIn(r"'C:\Users\me\mol.inp'", seen["cmd"])

    def test_a_path_wsl_cannot_see_is_reported_plainly(self):
        transport = self.transport()
        with patch.object(WSLTransport, "run", return_value=CommandResult(1, "", "no")):
            with self.assertRaises(TransportError) as caught:
                transport.to_wsl_path(r"C:\Users\me\mol.inp")
        self.assertIn("mounts", str(caught.exception))

    def test_a_quote_in_a_path_cannot_end_the_quoting(self):
        self.assertEqual(_quote("a'b"), "'a'\"'\"'b'")

    def test_a_distribution_without_bash_says_which_one(self):
        transport = self.transport(wsl_distro="docker-desktop")
        self.assertIn("docker-desktop", transport.no_shell_hint())
        self.assertIn("bash", transport.no_shell_hint())

    def test_without_wsl_the_hint_says_how_to_get_it(self):
        with patch("job_manager.transport.wsl.find_wsl", return_value=""):
            transport = WSLTransport(self.host())
            with self.assertRaises(TransportError) as caught:
                transport.run("echo hi")
        self.assertIn("wsl --install", str(caught.exception))

    def test_a_wsl_host_counts_as_this_machine(self):
        self.assertTrue(self.host().is_local)

    def test_the_target_names_the_distribution(self):
        self.assertEqual(self.host(wsl_distro="Ubuntu").target, "WSL: Ubuntu")
        self.assertEqual(self.host().target, "WSL")


if __name__ == "__main__":
    unittest.main()
