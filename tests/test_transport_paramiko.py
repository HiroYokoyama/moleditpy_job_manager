"""paramiko backend tests, driven by a fake ``paramiko`` module.

The real package is never imported: the backend is exercised against a stand-in
that records what it was asked to do, so these tests run identically whether or
not paramiko is installed.
"""

import os
import tempfile
import types
import unittest
from unittest.mock import patch

from job_manager.transport import create_transport, paramiko_available
from job_manager.transport.base import HostKeyRejected, TransportError

from .fakes import make_host


class FakeSSHException(Exception):
    pass


class FakeBadHostKeyException(FakeSSHException):
    pass


class FakeChannel:
    def __init__(self, rc=0):
        self._rc = rc

    def recv_exit_status(self):
        return self._rc


class FakeStream:
    def __init__(self, payload=b"", rc=0):
        self._payload = payload
        self.channel = FakeChannel(rc)

    def read(self):
        return self._payload


class FakeTransportHandle:
    def __init__(self, active=True):
        self._active = active

    def is_active(self):
        return self._active


class FakeSFTP:
    def __init__(self):
        self.puts = []
        self.gets = []
        self.closed = False
        self.raise_on_put = False

    def put(self, local, remote):
        if self.raise_on_put:
            raise OSError("disk full")
        self.puts.append((local, remote))

    def get(self, remote, local):
        self.gets.append((remote, local))
        with open(local, "w", encoding="utf-8") as handle:
            handle.write("x")

    def close(self):
        self.closed = True


class FakeSSHClient:
    instances = []

    def __init__(self):
        self.connect_kwargs = None
        self.policy = None
        self.loaded_system_keys = False
        self.loaded_files = []
        self.commands = []
        self.sftp = FakeSFTP()
        self.closed = False
        self.transport_handle = FakeTransportHandle()
        self.connect_error = None
        self.stdout_payload = b""
        self.rc = 0
        FakeSSHClient.instances.append(self)

    def load_system_host_keys(self):
        self.loaded_system_keys = True

    def load_host_keys(self, path):
        self.loaded_files.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def get_transport(self):
        return self.transport_handle

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        return None, FakeStream(self.stdout_payload, self.rc), FakeStream(b"")

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


class FakeRejectPolicy:
    pass


def make_fake_paramiko():
    module = types.ModuleType("paramiko")
    module.SSHClient = FakeSSHClient
    module.RejectPolicy = FakeRejectPolicy
    module.SSHException = FakeSSHException
    module.BadHostKeyException = FakeBadHostKeyException
    module.HostKeys = _FakeHostKeys
    module.Transport = _FakeTransportProbe
    return module


class _FakeHostKeys:
    saved = []

    def __init__(self):
        self.entries = []

    def load(self, path):
        pass

    def add(self, name, keytype, key):
        self.entries.append((name, keytype))

    def save(self, path):
        _FakeHostKeys.saved.append(path)


class _FakeKey:
    def get_name(self):
        return "ssh-ed25519"

    def get_fingerprint(self):
        return b"\x01\x02\x03\x04"


class _FakeTransportProbe:
    def __init__(self, sock):
        self.sock = sock

    def start_client(self, timeout=None):
        pass

    def get_remote_server_key(self):
        return _FakeKey()

    def close(self):
        pass


class ParamikoTestCase(unittest.TestCase):
    def setUp(self):
        FakeSSHClient.instances = []
        self.fake = make_fake_paramiko()
        self.patcher = patch("job_manager.transport.paramiko_backend.paramiko", self.fake)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        from job_manager.transport.paramiko_backend import ParamikoTransport

        self.cls = ParamikoTransport
        self.host = make_host(backend="paramiko")

    @property
    def client(self):
        return FakeSSHClient.instances[-1]


class TestAvailabilityFlag(unittest.TestCase):
    def test_module_imports_without_paramiko_installed(self):
        # CI has no paramiko; importing the backend must still work.
        from job_manager.transport import paramiko_backend

        self.assertIsInstance(paramiko_backend.PARAMIKO_AVAILABLE, bool)

    def test_install_hint_names_the_package(self):
        from job_manager.transport.paramiko_backend import INSTALL_HINT

        self.assertIn("pip install paramiko", INSTALL_HINT)

    def test_factory_reports_availability(self):
        self.assertIsInstance(paramiko_available(), bool)

    def test_factory_refuses_when_unavailable(self):
        with patch("job_manager.transport.paramiko_backend.PARAMIKO_AVAILABLE", False):
            with self.assertRaises(TransportError):
                create_transport(make_host(backend="paramiko"))

    def test_factory_builds_the_backend_when_available(self):
        with patch("job_manager.transport.paramiko_backend.PARAMIKO_AVAILABLE", True):
            transport = create_transport(make_host(backend="paramiko"), password="s3cret")
        self.assertEqual(transport.password, "s3cret")


class TestConnection(ParamikoTestCase):
    def test_host_keys_are_loaded_and_unknown_ones_rejected(self):
        transport = self.cls(self.host)
        transport.run("echo hi")
        self.assertTrue(self.client.loaded_system_keys)
        self.assertIsInstance(self.client.policy, FakeRejectPolicy)

    def test_connect_parameters(self):
        transport = self.cls(make_host(backend="paramiko", port=2222, username="alice"))
        transport.run("x")
        kwargs = self.client.connect_kwargs
        self.assertEqual(kwargs["port"], 2222)
        self.assertEqual(kwargs["username"], "alice")
        self.assertTrue(kwargs["allow_agent"])

    def test_password_is_passed_when_supplied(self):
        transport = self.cls(self.host, password="hunter2")
        transport.run("x")
        self.assertEqual(self.client.connect_kwargs["password"], "hunter2")

    def test_no_password_key_when_none_supplied(self):
        transport = self.cls(self.host)
        transport.run("x")
        self.assertNotIn("password", self.client.connect_kwargs)

    def test_key_path_is_expanded(self):
        transport = self.cls(make_host(backend="paramiko", key_path="~/id_rsa"))
        transport.run("x")
        self.assertNotIn("~", self.client.connect_kwargs["key_filename"])

    def test_unknown_host_key_becomes_host_key_rejected(self):
        transport = self.cls(self.host)
        error = FakeSSHException("Server 'x' not found in known_hosts")
        with patch.object(FakeSSHClient, "connect", side_effect=error):
            with self.assertRaises(HostKeyRejected):
                transport.run("x")

    def test_other_ssh_errors_become_transport_errors(self):
        transport = self.cls(self.host)
        with patch.object(FakeSSHClient, "connect", side_effect=FakeSSHException("no route")):
            with self.assertRaises(TransportError) as caught:
                transport.run("x")
        self.assertIn("no route", str(caught.exception))

    def test_socket_errors_become_transport_errors(self):
        transport = self.cls(self.host)
        with patch.object(FakeSSHClient, "connect", side_effect=OSError("refused")):
            with self.assertRaises(TransportError):
                transport.run("x")

    def test_the_connection_is_reused(self):
        transport = self.cls(self.host)
        transport.run("a")
        transport.run("b")
        self.assertEqual(len(FakeSSHClient.instances), 1)

    def test_a_dead_connection_is_rebuilt(self):
        transport = self.cls(self.host)
        transport.run("a")
        self.client.transport_handle = FakeTransportHandle(active=False)
        transport.run("b")
        self.assertEqual(len(FakeSSHClient.instances), 2)


class TestCommands(ParamikoTestCase):
    def test_output_and_exit_code(self):
        transport = self.cls(self.host)
        transport.run("x")
        self.client.stdout_payload = b"hello\n"
        self.client.rc = 7
        result = transport.run("y")
        self.assertEqual(result.stdout, "hello\n")
        self.assertEqual(result.rc, 7)

    def test_login_commands_are_prepended(self):
        host = make_host(backend="paramiko", login_commands=["source /etc/profile"])
        transport = self.cls(host)
        transport.run("squeue")
        self.assertEqual(self.client.commands[-1], "source /etc/profile; squeue")

    def test_exec_failures_are_wrapped(self):
        transport = self.cls(self.host)
        transport.run("x")
        with patch.object(FakeSSHClient, "exec_command", side_effect=RuntimeError("boom")):
            with self.assertRaises(TransportError):
                transport.run("y")

    def test_undecodable_output_does_not_crash(self):
        transport = self.cls(self.host)
        transport.run("x")
        self.client.stdout_payload = b"\xff\xfe bad bytes"
        self.assertIn("bad bytes", transport.run("y").stdout)


class TestTransfers(ParamikoTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="paramiko_")
        self.local = os.path.join(self.tmp, "a.inp")
        with open(self.local, "w", encoding="utf-8") as handle:
            handle.write("x")

    def test_upload(self):
        transport = self.cls(self.host)
        transport.upload(self.local, "/remote/a.inp")
        self.assertEqual(self.client.sftp.puts, [(self.local, "/remote/a.inp")])

    def test_tilde_is_resolved_because_sftp_has_no_shell(self):
        transport = self.cls(self.host)
        transport.run("x")
        self.client.stdout_payload = b"/home/tester\n"
        transport.upload(self.local, "~/jobs/a.inp")
        self.assertEqual(self.client.sftp.puts[0][1], "/home/tester/jobs/a.inp")

    def test_the_home_directory_is_resolved_once_per_connection(self):
        # Every remote path this plugin builds starts at ~/moleditpy_jobs, so
        # without a cache a submission ran one extra command per uploaded file
        # and a download one per fetched result, all asking the same question.
        transport = self.cls(self.host)
        transport.run("x")
        self.client.stdout_payload = b"/home/tester\n"

        transport.upload(self.local, "~/jobs/a.inp")
        transport.upload(self.local, "~/jobs/b.inp")
        transport.upload(self.local, "~/jobs/c.inp")

        self.assertEqual(len([c for c in self.client.commands if "$HOME" in c]), 1)

    def test_every_path_is_still_expanded_from_the_cache(self):
        transport = self.cls(self.host)
        transport.run("x")
        self.client.stdout_payload = b"/home/tester\n"

        transport.upload(self.local, "~/jobs/a.inp")
        transport.upload(self.local, "~/jobs/b.inp")

        self.assertEqual(
            [remote for _local, remote in self.client.sftp.puts],
            ["/home/tester/jobs/a.inp", "/home/tester/jobs/b.inp"],
        )

    def test_a_reconnection_asks_again(self):
        # A name can resolve to a different machine after a reconnect, and a
        # home directory learnt from the old one would send files elsewhere.
        transport = self.cls(self.host)
        transport.run("x")
        self.client.stdout_payload = b"/home/tester\n"
        transport.upload(self.local, "~/jobs/a.inp")

        transport.close()
        transport.run("x")
        self.client.stdout_payload = b"/scratch/tester\n"
        transport.upload(self.local, "~/jobs/b.inp")

        self.assertEqual(self.client.sftp.puts[-1][1], "/scratch/tester/jobs/b.inp")

    def test_unresolvable_home_raises(self):
        transport = self.cls(self.host)
        transport.run("x")
        self.client.stdout_payload = b""
        with self.assertRaises(TransportError):
            transport.upload(self.local, "~/jobs/a.inp")

    def test_upload_failure_is_wrapped(self):
        transport = self.cls(self.host)
        transport.upload(self.local, "/r/a")
        self.client.sftp.raise_on_put = True
        with self.assertRaises(TransportError):
            transport.upload(self.local, "/r/b")

    def test_download_creates_the_directory(self):
        transport = self.cls(self.host)
        target = os.path.join(self.tmp, "deep", "b.out")
        transport.download("/remote/b.out", target)
        self.assertTrue(os.path.exists(target))

    def test_close_releases_both_handles(self):
        transport = self.cls(self.host)
        transport.upload(self.local, "/r/a")
        sftp = self.client.sftp
        transport.close()
        self.assertTrue(sftp.closed)
        self.assertTrue(self.client.closed)

    def test_close_twice_is_safe(self):
        transport = self.cls(self.host)
        transport.close()
        transport.close()


class TestTrustHostKey(ParamikoTestCase):
    def test_appends_and_returns_a_fingerprint(self):
        from job_manager.transport import paramiko_backend

        tmp_home = tempfile.mkdtemp(prefix="knownhosts_")
        with patch.object(
            paramiko_backend, "known_hosts_path", lambda: os.path.join(tmp_home, "known_hosts")
        ):
            with patch("socket.create_connection", return_value=_DummySocket()):
                fingerprint = paramiko_backend.trust_host_key("h.example.org")
        self.assertEqual(fingerprint, "01020304")
        self.assertTrue(_FakeHostKeys.saved)

    def test_probe_failure_is_wrapped(self):
        from job_manager.transport import paramiko_backend

        with patch("socket.create_connection", side_effect=OSError("refused")):
            with self.assertRaises(TransportError):
                paramiko_backend.trust_host_key("h.example.org")

    def test_refuses_without_paramiko(self):
        from job_manager.transport import paramiko_backend

        with patch.object(paramiko_backend, "paramiko", None):
            with self.assertRaises(TransportError):
                paramiko_backend.trust_host_key("h")


class _DummySocket:
    def close(self):
        pass


REAL_PARAMIKO = None
try:
    import paramiko as REAL_PARAMIKO  # noqa: F401
except ImportError:
    pass

SSH_CONFIG = "\n".join(
    [
        "Host cluster",
        "  HostName login7.hpc.example.org",
        "  User alice",
        "  Port 2222",
        "  IdentityFile ~/.ssh/id_cluster",
        "",
    ]
)


class _StubParamiko:
    """Just enough of paramiko for ``_connect`` to reach ``client.connect``."""

    class SSHClient:
        last_kwargs = None

        def load_system_host_keys(self):
            pass

        def load_host_keys(self, path):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            type(self).last_kwargs = kwargs

    @staticmethod
    def RejectPolicy():
        return None

    SSHException = Exception
    BadHostKeyException = Exception
    # Config parsing is paramiko's own; faking it would test nothing.
    SSHConfig = getattr(REAL_PARAMIKO, "SSHConfig", None)


@unittest.skipUnless(REAL_PARAMIKO, "needs the real paramiko for SSHConfig")
class TestSshConfigIsHonoured(unittest.TestCase):
    """The OpenSSH backend gets ``~/.ssh/config`` for free by shelling out to
    ``ssh``. paramiko reads none of it, so a host that only works because of an
    alias, a per-host User, Port or IdentityFile failed here for reasons the
    user could not see."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sshconf_")
        os.makedirs(os.path.join(self.home, ".ssh"))
        self.expand = lambda path: path.replace("~", self.home, 1)

    def _write_config(self, text=SSH_CONFIG):
        target = os.path.join(self.home, ".ssh", "config")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _lookup(self, name):
        from job_manager.transport import paramiko_backend

        with patch("os.path.expanduser", self.expand):
            return paramiko_backend.ssh_config_for(name)

    def _connect_with(self, host):
        from job_manager.transport import paramiko_backend

        transport = paramiko_backend.ParamikoTransport(host)
        stub = _StubParamiko()
        with patch("os.path.expanduser", self.expand):
            with patch.object(paramiko_backend, "paramiko", stub):
                transport._connect()
        return stub.SSHClient.last_kwargs

    def test_alias_user_and_port_are_resolved(self):
        self._write_config()
        resolved = self._lookup("cluster")
        self.assertEqual(resolved.get("hostname"), "login7.hpc.example.org")
        self.assertEqual(resolved.get("user"), "alice")
        self.assertEqual(resolved.get("port"), "2222")

    def test_an_identity_file_is_resolved(self):
        self._write_config()
        self.assertTrue(self._lookup("cluster").get("identityfile"))

    def test_a_host_with_no_stanza_resolves_to_itself(self):
        self._write_config("Host other\n  HostName elsewhere\n")
        self.assertEqual(self._lookup("cluster").get("hostname"), "cluster")

    def test_a_missing_config_is_not_an_error(self):
        self.assertEqual(self._lookup("cluster"), {})

    def test_the_profile_wins_over_the_config(self):
        self._write_config()
        kwargs = self._connect_with(
            make_host(hostname="cluster", username="from_profile", port=2022)
        )
        self.assertEqual(kwargs["username"], "from_profile")
        self.assertEqual(kwargs["port"], 2022)

    def test_the_config_fills_in_what_the_profile_leaves_blank(self):
        self._write_config()
        kwargs = self._connect_with(make_host(hostname="cluster", username="", port=0))
        self.assertEqual(kwargs["hostname"], "login7.hpc.example.org")
        self.assertEqual(kwargs["username"], "alice")
        self.assertEqual(kwargs["port"], 2222)


class TestAJumpHostIsRefused(unittest.TestCase):
    """Silently ignoring the bastion would connect somewhere the profile did
    not ask for. ProxyJump is what the OpenSSH backend is for."""

    def _connect(self, host, config):
        from job_manager.transport import paramiko_backend

        transport = paramiko_backend.ParamikoTransport(host)
        with patch.object(paramiko_backend, "ssh_config_for", return_value=config):
            with patch.object(paramiko_backend, "paramiko", _StubParamiko()):
                transport._connect()

    def test_a_profile_jump_host_raises(self):
        with self.assertRaises(TransportError) as caught:
            self._connect(make_host(jump_host="user@bastion"), {})
        self.assertIn("jump host", str(caught.exception).lower())

    def test_a_proxyjump_from_the_config_also_raises(self):
        with self.assertRaises(TransportError):
            self._connect(make_host(), {"proxyjump": "bastion"})

    def test_a_plain_host_still_connects(self):
        self._connect(make_host(), {})


if __name__ == "__main__":
    unittest.main()


class TestParamikoIsNotChatty(unittest.TestCase):
    """It announced every connection and login at INFO, into the app's log."""

    def test_the_library_logger_is_quietened(self):
        import logging

        from job_manager.transport import paramiko_backend

        if not paramiko_backend.PARAMIKO_AVAILABLE:
            self.skipTest("paramiko is not installed")
        self.assertGreaterEqual(logging.getLogger("paramiko").level, logging.WARNING)

    def test_warnings_still_get_through(self):
        import logging

        from job_manager.transport import paramiko_backend

        if not paramiko_backend.PARAMIKO_AVAILABLE:
            self.skipTest("paramiko is not installed")
        self.assertLessEqual(logging.getLogger("paramiko").level, logging.WARNING)
