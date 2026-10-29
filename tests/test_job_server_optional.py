"""Optional HTTP destination: base imports remain dependency-free."""

import hashlib
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from job_manager.models import HostProfile, Job
from job_manager.server_backend import JobServerBackend, validate_server_url

ROOT = Path(__file__).resolve().parents[1]


class TestOptionalServerClient(unittest.TestCase):
    def test_default_server_log_uses_the_dedicated_log_endpoint(self):
        backend = object.__new__(JobServerBackend)
        backend.tail = Mock(return_value="server output")
        backend.client = Mock()
        job = Job(log_file="job.log")
        for filename in ("", "job.log"):
            with self.subTest(filename=filename):
                self.assertEqual(backend.tail_file(job, filename, 50), "server output")
                backend.tail.assert_called_with(job, 50)
        backend.client.request.assert_not_called()

    def test_disposable_backends_enable_shared_auth_but_keep_separate_clients(self):
        from job_manager.server_backend import backend_for

        client_type = Mock(side_effect=[Mock(), Mock()])
        host = HostProfile(
            backend="job_server", server_url="http://127.0.0.1:8766", server_key_path="fixture.key"
        )
        with (
            patch("job_manager.server_backend.require_client"),
            patch.dict(
                sys.modules,
                {
                    "job_manager.job_server_client.client": SimpleNamespace(
                        JobServerClient=client_type
                    )
                },
            ),
        ):
            first, second = backend_for(host), backend_for(host)
        self.assertIsNot(first.client, second.client)
        self.assertEqual(client_type.call_count, 2)
        self.assertTrue(
            all(call.kwargs == {"reuse_auth": True} for call in client_type.call_args_list)
        )

    def test_server_fetch_preserves_inputs_and_empty_patterns_download_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "input.xyz"
            original.write_text("original input", encoding="utf-8")
            job = Job(input_files=[str(original)], log_file="job.log")
            backend = object.__new__(JobServerBackend)
            backend.files = lambda _: [{"path": name} for name in ("input.xyz", "result.out")]

            def fetch(_, relative, target):
                Path(target).write_text("downloaded", encoding="utf-8")

            backend.fetch_file = Mock(side_effect=fetch)
            self.assertEqual(backend.fetch(job, directory, []), [str(root / "result.out")])
            self.assertEqual(original.read_text(encoding="utf-8"), "original input")
            self.assertEqual(backend.fetch(job, directory, ["input.xyz"]), [])

    def test_server_fetch_patterns_respect_subdirectories(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = object.__new__(JobServerBackend)
            backend.files = lambda _: [{"path": "result.out"}, {"path": "nested/result.out"}]
            backend.fetch_file = Mock()
            self.assertEqual(
                backend.fetch(Job(), directory, ["*.out"]), [str(Path(directory) / "result.out")]
            )

    def test_base_loads_and_reports_missing_optional_package_without_site_packages(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
import job_manager
from job_manager.models import HostProfile
from job_manager.server_backend import backend_for
assert job_manager.PLUGIN_DEPENDENCIES == []
assert 'cryptography' in job_manager.PLUGIN_OPTIONAL_DEPENDENCIES
assert not any(name.split('.')[0] in {'cryptography','fastapi','uvicorn','psutil','job_server','PySide6'} for name in sys.modules)
try:
    backend_for(HostProfile(backend='job_server', server_url='http://127.0.0.1:8766'))
except ValueError as error:
    assert 'optional cryptography' in str(error)
else:
    raise AssertionError('missing dependency must fail clearly')
"""
        subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT)], check=True, timeout=10)

    def test_snapshot_matches_audited_client_sources(self):
        directory = ROOT / "job_manager/job_server_client"
        manifest = json.loads((directory / "PROVENANCE.json").read_text("utf-8"))
        for name, expected in manifest["files"].items():
            self.assertEqual(
                hashlib.sha256((directory / name).read_text("utf-8").encode()).hexdigest(), expected
            )

    def test_server_profile_round_trip_and_no_shell_or_local_mirror(self):
        host = HostProfile(
            backend="job_server",
            server_url="https://compute.example.org",
            server_id="identity",
            server_key_path="device.key",
            equal_path="/example/mirror",
        )
        restored = HostProfile.from_dict(host.to_dict())
        self.assertEqual(restored.server_id, "identity")
        self.assertEqual(restored.target, host.server_url)
        self.assertFalse(restored.uses_remote_runner)
        self.assertEqual(restored.local_root(), "")
        self.assertEqual(HostProfile.from_dict({"name": "old"}).server_url, "")

    def test_unsafe_urls_rejected_without_crypto_import(self):
        for url in (
            "http://compute.example.org",
            "file:///example",
            "https://user:password@example.org",
            "https://example.org?token=x",
            "https://example.org/path",
            "https://example.org:99999",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_server_url(url)
        self.assertEqual(validate_server_url("http://127.0.0.1:8766/"), "http://127.0.0.1:8766")

    def test_bulk_poll_preserves_exit_and_reason_without_a_shell(self):
        backend = object.__new__(JobServerBackend)
        jobs = [Job(remote_job_id="job-a"), Job(remote_job_id="job-b")]
        from unittest.mock import Mock

        backend.client = Mock()
        backend.client.request.return_value = [
            {"id": "job-a", "state": "DONE", "exit_code": 0, "reason": ""},
            {"id": "job-b", "state": "RUNNING", "exit_code": None, "reason": "NEEDS_RECHECK"},
        ]
        self.assertEqual(backend.poll(jobs), {jobs[0].id: "DONE", jobs[1].id: "RUNNING"})
        self.assertEqual(jobs[0].rc, 0)
        self.assertEqual(jobs[1].last_error, "NEEDS_RECHECK")
        backend.client.request.assert_called_once_with("GET", "/jobs?limit=1000&offset=0")

    def test_old_crypto_is_treated_as_unavailable(self):
        from job_manager.server_backend import client_available

        with (
            patch("job_manager.server_backend.version", return_value="46.0.5"),
            patch("job_manager.server_backend.importlib.util.find_spec", return_value=object()),
        ):
            self.assertFalse(client_available())
