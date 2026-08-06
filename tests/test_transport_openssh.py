import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from job_manager.transport import create_transport
from job_manager.transport.base import CommandResult, HostKeyRejected, TransportError
from job_manager.transport.openssh import OpenSSHTransport

from .fakes import make_host


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class OpenSSHTestCase(unittest.TestCase):
    def setUp(self):
        self.host = make_host()
        self.transport = OpenSSHTransport(self.host)

    def run_with(self, *args, completed=None, side_effect=None, method="run"):
        with patch("subprocess.run") as mocked:
            if side_effect is not None:
                mocked.side_effect = side_effect
            else:
                mocked.return_value = completed or _Completed()
            result = getattr(self.transport, method)(*args)
        self.argv = mocked.call_args[0][0] if mocked.call_args else []
        self.kwargs = mocked.call_args[1] if mocked.call_args else {}
        return result


class TestArgumentAssembly(OpenSSHTestCase):
    def test_batch_mode_is_always_set(self):
        self.run_with("echo hi", method="run")
        self.assertIn("BatchMode=yes", self.argv)

    def test_target_and_command(self):
        self.run_with("echo hi", method="run")
        self.assertIn("tester@login.example.org", self.argv)
        self.assertEqual(self.argv[-1], "echo hi")
        self.assertEqual(self.argv[-2], "--")

    def test_default_port_is_not_passed(self):
        self.run_with("x", method="run")
        self.assertNotIn("-p", self.argv)

    def test_custom_port_uses_lowercase_p_for_ssh(self):
        self.transport = OpenSSHTransport(make_host(port=2222))
        self.run_with("x", method="run")
        self.assertIn("-p", self.argv)
        self.assertIn("2222", self.argv)

    def test_identity_file_forces_identities_only(self):
        self.transport = OpenSSHTransport(make_host(key_path="/keys/id_ed25519"))
        self.run_with("x", method="run")
        self.assertIn("/keys/id_ed25519", self.argv)
        self.assertIn("IdentitiesOnly=yes", self.argv)

    def test_jump_host(self):
        self.transport = OpenSSHTransport(make_host(jump_host="me@bastion"))
        self.run_with("x", method="run")
        self.assertIn("-J", self.argv)
        self.assertIn("me@bastion", self.argv)

    def test_extra_options_are_forwarded(self):
        self.transport = OpenSSHTransport(make_host(ssh_options=["ServerAliveInterval=30", " "]))
        self.run_with("x", method="run")
        self.assertIn("ServerAliveInterval=30", self.argv)

    def test_connect_timeout_from_the_profile(self):
        self.transport = OpenSSHTransport(make_host(connect_timeout=42))
        self.run_with("x", method="run")
        self.assertIn("ConnectTimeout=42", self.argv)

    def test_login_commands_are_prepended(self):
        self.transport = OpenSSHTransport(make_host(login_commands=["source /etc/profile"]))
        self.run_with("squeue", method="run")
        self.assertEqual(self.argv[-1], "source /etc/profile; squeue")

    def test_scp_uses_capital_p_for_the_port(self):
        self.transport = OpenSSHTransport(make_host(port=2222))
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            local = handle.name
        self.run_with(local, "/remote/x", method="upload")
        self.assertIn("-P", self.argv)
        self.assertNotIn("-p", self.argv)
        os.unlink(local)


class TestExecution(OpenSSHTestCase):
    def test_successful_command(self):
        result = self.run_with("x", completed=_Completed(0, "out", ""), method="run")
        self.assertIsInstance(result, CommandResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "out")

    def test_nonzero_exit_is_returned_not_raised(self):
        result = self.run_with("x", completed=_Completed(3, "", "boom"), method="run")
        self.assertEqual(result.rc, 3)
        self.assertFalse(result.ok)

    def test_missing_ssh_binary_is_explained(self):
        with self.assertRaises(TransportError) as caught:
            self.run_with("x", side_effect=FileNotFoundError(), method="run")
        self.assertIn("paramiko", str(caught.exception))

    def test_timeout_is_reported(self):
        error = subprocess.TimeoutExpired(cmd="ssh", timeout=5)
        with self.assertRaises(TransportError) as caught:
            self.run_with("x", side_effect=error, method="run")
        self.assertIn("timed out", str(caught.exception))

    def test_unknown_host_key_raises_a_dedicated_error(self):
        completed = _Completed(255, "", "Host key verification failed.")
        with self.assertRaises(HostKeyRejected):
            self.run_with("x", completed=completed, method="run")

    def test_password_prompt_failure_points_at_the_other_backend(self):
        completed = _Completed(255, "", "Permission denied (publickey,password).")
        with self.assertRaises(TransportError) as caught:
            self.run_with("x", completed=completed, method="run")
        self.assertIn("paramiko", str(caught.exception))

    def test_generic_255_is_not_swallowed_as_success(self):
        result = self.run_with(
            "x", completed=_Completed(255, "", "kex_exchange failed"), method="run"
        )
        self.assertEqual(result.rc, 255)


class TestTransfers(OpenSSHTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="scp_")
        self.local = os.path.join(self.tmp, "a.inp")
        with open(self.local, "w", encoding="utf-8") as handle:
            handle.write("x")

    def test_upload_builds_a_host_spec(self):
        self.run_with(self.local, "/remote/a.inp", method="upload")
        self.assertIn("tester@login.example.org:/remote/a.inp", self.argv)

    def test_upload_quotes_a_path_with_spaces(self):
        self.run_with(self.local, "/remote/dir with space/a.inp", method="upload")
        spec = [a for a in self.argv if a.startswith("tester@")][0]
        self.assertIn("'", spec)

    def test_upload_keeps_a_tilde_expandable(self):
        self.run_with(self.local, "~/jobs/a.inp", method="upload")
        spec = [a for a in self.argv if a.startswith("tester@")][0]
        self.assertTrue(spec.endswith(":~/jobs/a.inp"))

    def test_upload_failure_raises(self):
        with self.assertRaises(TransportError):
            self.run_with(
                self.local, "/r/a", completed=_Completed(1, "", "No such file"), method="upload"
            )

    def test_download_creates_the_local_directory(self):
        target = os.path.join(self.tmp, "deep", "b.out")
        self.run_with("/remote/b.out", target, method="download")
        self.assertTrue(os.path.isdir(os.path.dirname(target)))

    def test_download_failure_raises(self):
        target = os.path.join(self.tmp, "b.out")
        with self.assertRaises(TransportError):
            self.run_with(
                "/r/b", target, completed=_Completed(1, "", "not found"), method="download"
            )


class TestMultiplexing(OpenSSHTestCase):
    def test_control_master_only_off_windows(self):
        with patch("job_manager.transport.openssh.SUPPORTS_MULTIPLEXING", True):
            transport = OpenSSHTransport(self.host)
            self.assertIsNotNone(transport._control_path())
        with patch("job_manager.transport.openssh.SUPPORTS_MULTIPLEXING", False):
            transport = OpenSSHTransport(self.host)
            self.assertIsNone(transport._control_path())

    def test_close_is_safe_without_a_connection(self):
        with patch("job_manager.transport.openssh.SUPPORTS_MULTIPLEXING", False):
            OpenSSHTransport(self.host).close()

    def test_close_tears_down_the_master(self):
        with patch("job_manager.transport.openssh.SUPPORTS_MULTIPLEXING", True):
            transport = OpenSSHTransport(self.host)
            transport._control_path()
            with patch("subprocess.run") as mocked:
                transport.close()
            self.assertIn("-O", mocked.call_args[0][0])
        self.assertIsNone(transport._control_dir)


class TestTestConnection(OpenSSHTestCase):
    def test_returns_the_remote_hostname(self):
        result = self.run_with(
            completed=_Completed(0, "moleditpy_ok\nnode01.cluster\n"),
            method="test_connection",
        )
        self.assertEqual(result, "node01.cluster")

    def test_failure_raises(self):
        with self.assertRaises(TransportError):
            self.run_with(completed=_Completed(1, "", "denied"), method="test_connection")


class TestFactory(unittest.TestCase):
    def test_openssh_is_the_default(self):
        self.assertIsInstance(create_transport(make_host()), OpenSSHTransport)

    def test_unknown_backend_falls_back_to_openssh(self):
        self.assertIsInstance(
            create_transport(make_host(backend="carrier-pigeon")), OpenSSHTransport
        )


if __name__ == "__main__":
    unittest.main()
