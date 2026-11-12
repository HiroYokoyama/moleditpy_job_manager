"""known_hosts lines that are malformed, negated or too short.

Synthetic files only: nothing here reads the real ``~/.ssh``.
"""

import base64
import os
import tempfile
import unittest

from job_manager import known_hosts

BLOB = base64.b64encode(b"key-a").decode("ascii")


class KnownHostsEdgeTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix="_known_hosts")
        os.close(handle)
        self.addCleanup(os.remove, self.path)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_malformed_hashed_pattern_matches_nothing(self):
        for pattern in ("|1|only-two", "|1|!!notbase64!!|AAAA", "|1|AAAA|%%%"):
            with self.subTest(pattern=pattern):
                self.assertFalse(known_hosts._hashed_match(pattern, "myhost"))
        self.write(f"|1|!!notbase64!!|AAAA ssh-ed25519 {BLOB}\n")
        self.assertEqual(known_hosts.keys_for(self.path, "myhost"), [])

    def test_a_negated_pattern_excludes_a_host_a_wildcard_would_match(self):
        self.write(f"*.example.org,!bad.example.org ssh-ed25519 {BLOB}\n")
        self.assertEqual(
            known_hosts.keys_for(self.path, "good.example.org"), [("ssh-ed25519", BLOB)]
        )
        self.assertEqual(known_hosts.keys_for(self.path, "bad.example.org"), [])

    def test_a_line_with_too_few_fields_is_skipped(self):
        self.write(f"myhost ssh-ed25519\n@revoked myhost\nmyhost ssh-ed25519 {BLOB}\n")
        entries = known_hosts.parse(self.path)
        self.assertEqual(
            [(e.hosts, e.keytype, e.blob) for e in entries], [("myhost", "ssh-ed25519", BLOB)]
        )


if __name__ == "__main__":
    unittest.main()
