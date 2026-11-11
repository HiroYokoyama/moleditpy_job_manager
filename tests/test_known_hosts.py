"""known_hosts is appended to, never rewritten, and a changed key is never trusted.

Synthetic files only: nothing here reads the real ``~/.ssh``.
"""

import base64
import hashlib
import hmac
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from job_manager import known_hosts
from job_manager.transport.base import (
    HOST_KEY_PROBLEM_CHANGED,
    HOST_KEY_PROBLEM_UNKNOWN,
    HostKeyChanged,
    HostKeyRejected,
    TransportError,
    host_key_problem,
)
from job_manager.transport.openssh import OpenSSHTransport

from .fakes import make_host

BLOB_A = base64.b64encode(b"key-a").decode("ascii")
BLOB_B = base64.b64encode(b"key-b").decode("ascii")

CHANGED_STDERR = "\n".join(
    [
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
        "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
        "Add correct host key in /home/alice/.ssh/known_hosts to get rid of this message.",
        "Offending ED25519 key in /home/alice/.ssh/known_hosts:12",
        "Host key for myhost has changed and you have requested strict checking.",
        "Host key verification failed.",
    ]
)
UNKNOWN_STDERR = (
    "No ED25519 host key is known for myhost and you have requested strict checking.\n"
    "Host key verification failed."
)


class KnownHostsFile(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="known_hosts_")
        self.path = os.path.join(self.folder, "ssh", "known_hosts")

    def write(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def read(self) -> str:
        with open(self.path, encoding="utf-8", newline="") as handle:
            return handle.read()


class TestAppend(KnownHostsFile):
    def test_everything_already_in_the_file_survives_byte_for_byte(self):
        original = (
            "# a comment the user wrote\n"
            f"other.example.org ssh-ed25519 {BLOB_B} a trailing comment\n"
            f"fido.example.org sk-ssh-ed25519@openssh.com {BLOB_B}\n"
            f"@cert-authority *.example.org ssh-ed25519 {BLOB_B}\n"
            f"@revoked * ssh-rsa {BLOB_B}\n"
            "a line nobody can parse\n"
        )
        self.write(original)
        self.assertTrue(known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_A))
        self.assertEqual(self.read(), original + f"myhost ssh-ed25519 {BLOB_A}\n")

    def test_a_file_without_a_final_newline_does_not_get_a_joined_line(self):
        self.write(f"other.example.org ssh-ed25519 {BLOB_B}")
        known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_A)
        self.assertEqual(
            self.read().splitlines(),
            [f"other.example.org ssh-ed25519 {BLOB_B}", f"myhost ssh-ed25519 {BLOB_A}"],
        )

    def test_a_missing_file_and_folder_are_created(self):
        self.assertTrue(known_hosts.append(self.path, "myhost", 2222, "ssh-ed25519", BLOB_A))
        self.assertEqual(self.read(), f"[myhost]:2222 ssh-ed25519 {BLOB_A}\n")

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_a_new_file_is_private(self):
        known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_A)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_the_same_key_is_not_added_twice(self):
        self.write(f"myhost ssh-ed25519 {BLOB_A}\n")
        self.assertFalse(known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_A))
        self.assertEqual(self.read(), f"myhost ssh-ed25519 {BLOB_A}\n")

    def test_a_different_key_for_a_known_host_is_refused_not_replaced(self):
        self.write(f"myhost ssh-ed25519 {BLOB_A}\n")
        with self.assertRaises(known_hosts.KnownHostConflict):
            known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_B)
        self.assertEqual(self.read(), f"myhost ssh-ed25519 {BLOB_A}\n")

    def test_a_hashed_entry_counts_as_the_host_it_hashes(self):
        salt = b"0123456789abcdefghij"
        digest = hmac.new(salt, b"myhost", hashlib.sha1).digest()
        hashed = "|1|{}|{}".format(
            base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")
        )
        self.write(f"{hashed} ssh-ed25519 {BLOB_A}\n")
        with self.assertRaises(known_hosts.KnownHostConflict):
            known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_B)
        self.assertEqual(known_hosts.keys_for(self.path, "myhost"), [("ssh-ed25519", BLOB_A)])
        self.assertEqual(known_hosts.keys_for(self.path, "elsewhere"), [])

    def test_another_port_of_the_same_machine_is_another_host(self):
        self.write(f"myhost ssh-ed25519 {BLOB_A}\n")
        self.assertTrue(known_hosts.append(self.path, "myhost", 2222, "ssh-ed25519", BLOB_B))

    def test_another_key_type_is_added_beside_the_first(self):
        self.write(f"myhost ssh-rsa {BLOB_A}\n")
        self.assertTrue(known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_B))

    def test_a_revoked_key_is_refused(self):
        self.write(f"@revoked * ssh-ed25519 {BLOB_A}\n")
        with self.assertRaises(known_hosts.KnownHostConflict):
            known_hosts.append(self.path, "myhost", 22, "ssh-ed25519", BLOB_A)

    def test_a_comma_list_and_a_wildcard_match(self):
        self.write(f"myhost,192.0.2.7 ssh-ed25519 {BLOB_A}\n*.example.org ssh-ed25519 {BLOB_B}\n")
        self.assertEqual(known_hosts.keys_for(self.path, "192.0.2.7"), [("ssh-ed25519", BLOB_A)])
        self.assertEqual(
            known_hosts.keys_for(self.path, "login.example.org"), [("ssh-ed25519", BLOB_B)]
        )


class TestUsable(KnownHostsFile):
    def test_revoked_and_authority_lines_are_not_plain_keys(self):
        self.write(
            f"myhost ssh-ed25519 {BLOB_A}\n"
            f"other ssh-ed25519 {BLOB_B}\n"
            f"@revoked * ssh-ed25519 {BLOB_B}\n"
            f"@cert-authority * ssh-ed25519 {BLOB_A}\n"
        )
        taken = known_hosts.usable(known_hosts.parse(self.path))
        self.assertEqual([(entry.hosts, entry.blob) for entry in taken], [("myhost", BLOB_A)])

    def test_an_unreadable_file_is_an_empty_one(self):
        self.assertEqual(known_hosts.parse(self.path), [])


class TestWhichRefusal(unittest.TestCase):
    def test_sshs_changed_key_warning_is_changed_although_it_names_the_file(self):
        self.assertEqual(host_key_problem(CHANGED_STDERR), HOST_KEY_PROBLEM_CHANGED)

    def test_sshs_batch_mode_refusal_is_unknown(self):
        self.assertEqual(host_key_problem(UNKNOWN_STDERR), HOST_KEY_PROBLEM_UNKNOWN)

    def test_paramikos_wording_is_unknown(self):
        self.assertEqual(
            host_key_problem("Server 'myhost' not found in known_hosts"), HOST_KEY_PROBLEM_UNKNOWN
        )

    def test_our_own_changed_error_is_recognised_from_its_text(self):
        self.assertEqual(
            host_key_problem(str(HostKeyChanged("myhost", "detail"))), HOST_KEY_PROBLEM_CHANGED
        )

    def test_an_ordinary_failure_is_neither(self):
        self.assertEqual(host_key_problem("Connection refused"), "")
        self.assertEqual(host_key_problem(""), "")


class _Completed:
    def __init__(self, returncode, stderr):
        self.returncode, self.stdout, self.stderr = returncode, "", stderr


class TestOpenSSH(unittest.TestCase):
    def run_with(self, stderr):
        transport = OpenSSHTransport(make_host())
        with patch("job_manager.transport.openssh.SUPPORTS_MULTIPLEXING", False):
            with patch.object(subprocess, "run", return_value=_Completed(255, stderr)):
                transport.run("true")

    def test_a_changed_key_raises_its_own_error(self):
        with self.assertRaises(HostKeyChanged):
            self.run_with(CHANGED_STDERR)

    def test_an_unknown_key_is_not_reported_as_changed(self):
        with self.assertRaises(HostKeyRejected) as caught:
            self.run_with(UNKNOWN_STDERR)
        self.assertNotIsInstance(caught.exception, HostKeyChanged)

    def test_a_destination_that_reads_as_an_option_never_reaches_ssh(self):
        for field in ("hostname", "username"):
            host = make_host(**{field: "-oProxyCommand=mycommand"})
            transport = OpenSSHTransport(host)
            with patch("job_manager.transport.openssh.SUPPORTS_MULTIPLEXING", False):
                with patch.object(subprocess, "run") as spawned:
                    with self.assertRaises(TransportError):
                        transport.run("true")
                    with self.assertRaises(TransportError):
                        transport.upload("a", "b")
                    with self.assertRaises(TransportError):
                        transport.download("a", os.path.join(tempfile.gettempdir(), "b"))
                spawned.assert_not_called()


try:
    import paramiko as REAL_PARAMIKO
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    REAL_PARAMIKO = None


def _public_line() -> str:
    return (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode("ascii")
    )


@unittest.skipUnless(REAL_PARAMIKO, "needs the real paramiko")
class TestWithRealParamiko(KnownHostsFile):
    def setUp(self):
        super().setUp()
        from job_manager.transport import paramiko_backend

        self.backend = paramiko_backend
        patcher = patch.object(paramiko_backend, "known_hosts_path", lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def key(self, line: str):
        keytype, blob = line.split()[:2]
        return REAL_PARAMIKO.PKey.from_type_string(keytype, base64.b64decode(blob))

    def test_marker_lines_no_longer_stop_the_keys_after_them_loading(self):
        mine, other = _public_line(), _public_line()
        self.write(
            f"@cert-authority *.example.org {other}\n"
            f"@revoked * {other}\n"
            f"fido.example.org sk-ssh-ed25519@openssh.com {BLOB_A}\n"
            f"myhost {mine}\n"
        )
        store = REAL_PARAMIKO.HostKeys()
        self.assertEqual(self.backend.load_known_hosts(store), 1)
        self.assertTrue(store.check("myhost", self.key(mine)))

    def test_a_revoked_key_is_not_loaded_as_known(self):
        mine = _public_line()
        self.write(f"myhost {mine}\n@revoked * {mine}\n")
        store = REAL_PARAMIKO.HostKeys()
        self.assertEqual(self.backend.load_known_hosts(store), 0)
        self.assertIsNone(store.lookup("myhost"))

    def test_accepting_a_key_keeps_what_paramiko_cannot_parse(self):
        other, new = _public_line(), _public_line()
        original = (
            "# sites\n"
            f"@cert-authority *.example.org {other}\n"
            f"fido.example.org sk-ssh-ed25519@openssh.com {BLOB_A}\n"
        )
        self.write(original)
        self.backend.add_host_key(self.key(new), "myhost", 22)
        self.assertEqual(self.read(), original + f"myhost {new}\n")

    def test_a_changed_key_cannot_be_filed_over_the_old_one(self):
        old, new = _public_line(), _public_line()
        self.write(f"myhost {old}\n")
        with self.assertRaises(HostKeyChanged):
            self.backend.add_host_key(self.key(new), "myhost", 22)
        self.assertEqual(self.read(), f"myhost {old}\n")


if __name__ == "__main__":
    unittest.main()
