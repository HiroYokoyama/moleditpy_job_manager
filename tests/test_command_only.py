"""Jobs with no input file, and jobs that run in a directory already on the host.

Two related cases the plugin used to refuse outright: work staged on the
cluster by hand, and a command that simply has no input file of its own. The
interesting part is not that they are allowed now, but that a *shared*
directory changes what the wrapper may be called -- see
:func:`job_manager.runner.name_job_files`.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from job_manager import dialect, runner
from job_manager.models import SENTINEL_NAME, Job, SubmitPreset
from job_manager.schedulers import get_scheduler
from job_manager.schedulers.base import format_command, placeholder_values, references_input
from job_manager.transport.base import TransportError

from .fakes import FakeTransport, make_host, make_preset

from .bash_support import find_bash

BASH = find_bash()

#: A fake host answers "not there" to every existence check by default, which
#: is what an unprepared directory looks like; tests that want a directory to
#: exist say so.
PRESENT = dialect.PRESENT


def present(transport: FakeTransport) -> FakeTransport:
    """Make every path this transport is asked about exist."""
    return transport.when("PRESENT", stdout=f"{PRESENT}\n")


def make_transport(host=None) -> FakeTransport:
    return FakeTransport(host).when("sbatch", stdout="777\n")


class TestSubmittingWithNoInputFile(unittest.TestCase):
    """The command is the job; there is no file to upload."""

    def setUp(self):
        self.host = make_host()
        self.transport = make_transport(self.host)
        self.job = Job(id="cmd1", name="sweep", host_id=self.host.id)

    def submit(self, preset=None, **job_fields):
        for key, value in job_fields.items():
            setattr(self.job, key, value)
        return runner.submit_job(
            self.transport,
            self.host,
            preset or make_preset(command_template="./run_all.sh"),
            self.job,
            [],
        )

    def test_it_submits_with_no_local_files(self):
        job = self.submit()
        self.assertEqual(job.remote_job_id, "777")

    def test_only_the_wrapper_is_uploaded(self):
        self.submit()
        uploaded = [remote for _local, remote in self.transport.uploads]
        self.assertEqual(len(uploaded), 1)
        self.assertTrue(uploaded[0].endswith("moleditpy_run.sh"))

    def test_the_command_is_in_the_script(self):
        job = self.submit()
        self.assertIn("./run_all.sh", job.command)

    def test_an_empty_input_leaves_no_stray_placeholder_text(self):
        # {stem} of nothing is nothing -- not the word "None", and not the
        # tag left in place, either of which would run as a filename.
        job = self.submit(preset=make_preset(command_template="prog {input} > {stem}.out"))
        payload = job.command.strip().splitlines()[-1]
        self.assertEqual(payload, "prog  > .out")

    def test_a_job_with_no_command_is_refused(self):
        with self.assertRaises(ValueError):
            self.submit(preset=make_preset(command_template="   "))

    def test_it_still_gets_its_own_directory(self):
        job = self.submit()
        self.assertIn("sweep", job.remote_dir)
        self.assertTrue(self.transport.ran("mkdir -p"))

    def test_the_shared_names_are_kept_where_nothing_is_shared(self):
        job = self.submit()
        self.assertEqual(job.log_file, runner.DEFAULT_LOG_NAME)
        self.assertEqual(job.sentinel_name, "")
        self.assertEqual(runner.sentinel_for(job), SENTINEL_NAME)


class TestADirectoryTheUserPrepared(unittest.TestCase):
    def setUp(self):
        self.host = make_host()
        self.transport = present(make_transport(self.host))
        self.preset = make_preset(command_template="orca {input} > {stem}.out")

    def make_job(self, **fields):
        defaults = dict(
            id="ab12",
            name="mol42",
            host_id=self.host.id,
            remote_dir="~/runs/mol42",
            remote_dir_provided=True,
            remote_input="mol.inp",
        )
        defaults.update(fields)
        return Job(**defaults)

    def submit(self, job=None, files=()):
        job = job or self.make_job()
        return runner.submit_job(self.transport, self.host, self.preset, job, list(files))

    def test_the_directory_is_checked_and_not_created(self):
        self.submit()
        self.assertTrue(self.transport.ran("-d ~/runs/mol42"))
        self.assertFalse(self.transport.ran("mkdir -p ~/runs/mol42"))

    def test_the_job_runs_where_it_was_told_to(self):
        job = self.submit()
        self.assertEqual(job.remote_dir, "~/runs/mol42")
        self.assertIn("cd ~/runs/mol42", job.command)

    def test_a_directory_that_is_not_there_stops_the_submission(self):
        transport = make_transport(self.host)  # answers nothing to every check
        with self.assertRaises(TransportError) as caught:
            runner.submit_job(transport, self.host, self.preset, self.make_job(), [])
        self.assertIn("~/runs/mol42", str(caught.exception))
        # And nothing was uploaded into whatever that path turned out to be.
        self.assertEqual(transport.uploads, [])

    def test_a_named_input_that_is_not_there_stops_the_submission(self):
        transport = make_transport(self.host)
        # The directory is there, the file is not.
        transport.when("-f ~/runs/mol42/mol.inp", stdout=f"{dialect.MISSING}\n")
        transport.when("-d ~/runs/mol42", stdout=f"{PRESENT}\n")
        with self.assertRaises(TransportError) as caught:
            runner.submit_job(transport, self.host, self.preset, self.make_job(), [])
        self.assertIn("mol.inp", str(caught.exception))

    def test_the_named_input_fills_the_placeholders(self):
        job = self.submit()
        self.assertIn("orca mol.inp > mol.out", job.command)

    def test_a_local_file_is_uploaded_into_that_same_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "extra.dat")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            self.submit(files=[path])
        remote = [target for _local, target in self.transport.uploads]
        self.assertIn("~/runs/mol42/extra.dat", remote)

    def test_a_name_on_the_host_beats_an_uploaded_file(self):
        # Both are given: the one the user typed is the explicit answer.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "other.inp")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            job = self.submit(files=[path])
        self.assertIn("orca mol.inp", job.command)

    # --- the names everything the wrapper writes takes --------------------

    def test_everything_written_there_carries_the_job_id(self):
        job = self.submit()
        self.assertEqual(job.script_name, "moleditpy_run_ab12.sh")
        self.assertEqual(job.log_file, "moleditpy_ab12.log")
        self.assertEqual(job.sentinel_name, f"{SENTINEL_NAME}_ab12")

    def test_the_script_writes_its_own_sentinel(self):
        job = self.submit()
        self.assertIn(f"{SENTINEL_NAME}_ab12", job.command)
        # And not the shared one, which another job in the same directory
        # would be reading as its own result.
        self.assertNotIn(f"rm -f {SENTINEL_NAME}\n", job.command)

    def test_the_wrapper_is_uploaded_and_submitted_under_that_name(self):
        self.submit()
        uploaded = [remote for _local, remote in self.transport.uploads]
        self.assertIn("~/runs/mol42/moleditpy_run_ab12.sh", uploaded)
        self.assertTrue(self.transport.ran("sbatch --parsable moleditpy_run_ab12.sh"))

    def test_two_jobs_in_one_directory_share_nothing(self):
        first = self.submit(self.make_job(id="aaaa"))
        second = self.submit(self.make_job(id="bbbb"))
        self.assertNotEqual(first.script_name, second.script_name)
        self.assertNotEqual(first.log_file, second.log_file)
        self.assertNotEqual(first.sentinel_name, second.sentinel_name)

    def test_the_sentinel_is_read_back_under_the_same_name(self):
        job = self.submit()
        job.state = "RUNNING"
        transport = present(make_transport(self.host))
        transport.when("cat", stdout="@@MOLEDITPY@@\n0\n")
        runner._read_sentinels(transport, [job])
        self.assertTrue(transport.ran(f"~/runs/mol42/{SENTINEL_NAME}_ab12"))

    def test_an_older_job_still_reads_the_shared_name(self):
        # jobs.json written before this existed has no sentinel_name at all.
        old = Job(id="old1", remote_dir="~/jobs/old1")
        self.assertEqual(runner.sentinel_for(old), SENTINEL_NAME)
        self.assertEqual(runner.script_name_for(old, get_scheduler("slurm")), "moleditpy_run.sh")


class TestTheHelperQueuePath(unittest.TestCase):
    """The same two cases, on a host whose concurrency is kept by the runner."""

    def setUp(self):
        self.host = make_host(scheduler="shell", max_concurrent=2)
        self.assertTrue(self.host.uses_remote_runner)
        self.transport = present(FakeTransport(self.host))

    def submit(self, job):
        return runner.submit_to_runner(
            self.transport,
            self.host,
            make_preset(command_template="./run_all.sh"),
            job,
            [],
        )

    def test_it_queues_a_job_with_no_input_file(self):
        job = self.submit(Job(id="q1", name="sweep"))
        self.assertTrue(job.remote_job_id)

    def test_the_queued_entry_runs_the_per_job_wrapper(self):
        job = self.submit(
            Job(id="q2", name="sweep", remote_dir="~/runs/mol42", remote_dir_provided=True)
        )
        self.assertEqual(job.script_name, "moleditpy_run_q2.sh")
        queued = "\n".join(self.transport.uploaded_text.values())
        self.assertIn("moleditpy_run_q2.sh", queued)


class TestTheNewPlaceholders(unittest.TestCase):
    def test_the_job_name_and_directory_are_available(self):
        values = placeholder_values("", SubmitPreset(), "sweep", "~/runs/mol42")
        self.assertEqual(values["name"], "sweep")
        self.assertEqual(values["jobdir"], "~/runs/mol42")

    def test_they_are_substituted_in_a_command(self):
        command = format_command(
            "tar czf {name}.tar.gz {jobdir}", "", SubmitPreset(), "sweep", "~/runs/mol42"
        )
        self.assertEqual(command, "tar czf sweep.tar.gz ~/runs/mol42")

    def test_the_square_spelling_works_too(self):
        self.assertEqual(
            format_command("echo [name]", "", SubmitPreset(), "sweep", ""), "echo sweep"
        )

    def test_they_reach_the_script(self):
        script = get_scheduler("slurm").build_script(
            "sweep",
            SubmitPreset(command_template="./go.sh {jobdir}"),
            "",
            "job.log",
            remote_dir="~/runs/mol42",
        )
        self.assertIn("./go.sh ~/runs/mol42", script)


class TestSpottingACommandThatNeedsAnInput(unittest.TestCase):
    """`orca {input}` with no input substitutes to `orca ` and fails on the
    host; the wizard refuses it instead."""

    def test_the_input_tags(self):
        for template in (
            "orca {input} > {stem}.out",
            "g16 [input]",
            "cp {basename} /tmp",
            "prog > {output}",
        ):
            self.assertTrue(references_input(template), template)

    def test_a_command_that_names_its_own_files(self):
        for template in ("./run_all.sh", "vasp_std > vasp.out", "tar czf {name}.tgz {jobdir}", ""):
            self.assertFalse(references_input(template), template)

    def test_shell_syntax_that_only_looks_like_a_tag(self):
        # The same false positives format_command has to avoid.
        self.assertFalse(references_input("awk '{print $1}' x"))
        self.assertFalse(references_input("if [ -f input ]; then ./go.sh; fi"))


class TestTheJobNameReachingACommand(unittest.TestCase):
    def script(self, name):
        return get_scheduler("slurm").build_script(
            name, SubmitPreset(command_template="echo {name}"), "", "job.log"
        )

    def test_it_is_sanitised_like_everything_else_that_is_interpolated(self):
        # Not "echo my job; rm -rf ~": {name} lands in a command line.
        self.assertIn("echo my_job_rm_-rf", self.script("my job; rm -rf ~"))

    def test_the_directive_and_the_command_agree(self):
        script = self.script("opt run")
        self.assertIn("--job-name=opt_run", script)
        self.assertIn("echo opt_run", script)

    def test_the_windows_wrapper_agrees_too(self):
        script = get_scheduler("windows").build_script(
            "opt run", SubmitPreset(command_template="echo {name}"), "", "job.log"
        )
        self.assertIn("echo opt_run", script)
        self.assertIn("# job: opt_run", script)


class TestTheWindowsWrapper(unittest.TestCase):
    def script(self, sentinel=""):
        return get_scheduler("windows").build_script(
            "j",
            SubmitPreset(command_template="prog.exe"),
            "",
            "job.log",
            remote_dir="C:/runs/mol42",
            sentinel=sentinel,
        )

    def test_the_default_is_still_the_shared_name(self):
        self.assertIn(f"'{SENTINEL_NAME}'", self.script())

    def test_a_per_job_sentinel_is_used_throughout(self):
        script = self.script(f"{SENTINEL_NAME}_ab12")
        self.assertIn(f"'{SENTINEL_NAME}_ab12'", script)
        self.assertIn(f"'{SENTINEL_NAME}_ab12.tmp'", script)
        self.assertNotIn(f"'{SENTINEL_NAME}'", script)


class TestTheExistenceCheck(unittest.TestCase):
    def test_posix_asks_about_a_directory(self):
        command = dialect.POSIX.exists("~/runs/mol 42", directory=True)
        self.assertIn("-d ~/'runs/mol 42'", command)
        self.assertIn(PRESENT, command)
        self.assertIn(dialect.MISSING, command)

    def test_posix_asks_about_a_file(self):
        self.assertIn("-f ", dialect.POSIX.exists("~/runs/mol.inp"))

    def test_powershell_asks_in_its_own_language(self):
        command = dialect.POWERSHELL.exists("C:/runs", directory=True)
        self.assertIn("Test-Path", command)
        self.assertIn("-PathType Container", command)
        self.assertNotIn("[ -d", command)

    def test_a_missing_answer_is_not_read_as_present(self):
        # The word MISSING contains no "PRESENT", but a substring check the
        # other way round would have matched it.
        self.assertNotIn(PRESENT, dialect.MISSING)


@unittest.skipUnless(BASH, "no bash available")
class TestTwoRealScriptsInOneDirectory(unittest.TestCase):
    """The collision the per-job names exist to prevent, run for real."""

    def run_script(self, workdir, sentinel, command):
        script = get_scheduler("shell").build_script(
            "t",
            SubmitPreset(command_template=command),
            "",
            "job.log",
            sentinel=sentinel,
        )
        path = os.path.join(workdir, f"run_{sentinel}.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        subprocess.run([BASH, path], capture_output=True, timeout=60, cwd=workdir)

    def read(self, workdir, name):
        with open(os.path.join(workdir, name), encoding="utf-8") as handle:
            return handle.read().strip()

    def test_each_job_records_its_own_exit_code(self):
        workdir = tempfile.mkdtemp(prefix="shared_dir_")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.run_script(workdir, f"{SENTINEL_NAME}_aaaa", "true")
        self.run_script(workdir, f"{SENTINEL_NAME}_bbbb", "exit 7")
        self.assertEqual(self.read(workdir, f"{SENTINEL_NAME}_aaaa"), "0")
        self.assertEqual(self.read(workdir, f"{SENTINEL_NAME}_bbbb"), "7")

    def test_a_command_only_script_runs_with_no_input_file(self):
        workdir = tempfile.mkdtemp(prefix="command_only_")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.run_script(workdir, f"{SENTINEL_NAME}_cccc", "echo done > result.txt")
        self.assertEqual(self.read(workdir, "result.txt"), "done")
        self.assertEqual(self.read(workdir, f"{SENTINEL_NAME}_cccc"), "0")


if __name__ == "__main__":
    unittest.main()
