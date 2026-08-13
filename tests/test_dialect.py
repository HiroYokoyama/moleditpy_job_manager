"""The housekeeping commands, in both shells.

Making a directory, reading a sentinel, listing outputs, tailing a log: none of
them the job, all of them required, and all of them POSIX until the native
Windows backend arrived. A wrapper written in PowerShell is no use if the
plugin then asks the host for ``mkdir -p``.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from job_manager import dialect
from job_manager.models import SCHEDULER_SHELL, SCHEDULER_SLURM, SCHEDULER_WINDOWS, HostProfile

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class TestChoosingOne(unittest.TestCase):
    def test_a_windows_host_speaks_powershell(self):
        host = HostProfile(scheduler=SCHEDULER_WINDOWS)
        self.assertIs(dialect.for_host(host), dialect.POWERSHELL)

    def test_every_other_scheduler_speaks_posix(self):
        for name in (SCHEDULER_SLURM, SCHEDULER_SHELL, "pbs", "sge"):
            with self.subTest(scheduler=name):
                self.assertIs(dialect.for_host(HostProfile(scheduler=name)), dialect.POSIX)

    def test_something_without_a_scheduler_does_not_crash(self):
        self.assertIs(dialect.for_host(object()), dialect.POSIX)


class TestPosixQuoting(unittest.TestCase):
    speak = dialect.POSIX

    def test_a_tilde_still_expands(self):
        # shlex.quote("~/jobs") gives a literal directory named ~.
        self.assertEqual(self.speak.quote("~/jobs"), "~/jobs")

    def test_a_space_after_the_tilde_is_quoted(self):
        self.assertEqual(self.speak.quote("~/my jobs"), "~/'my jobs'")

    def test_a_bare_tilde_is_left_alone(self):
        self.assertEqual(self.speak.quote("~"), "~")


class TestPowerShellQuoting(unittest.TestCase):
    speak = dialect.POWERSHELL

    def test_a_single_quote_is_doubled(self):
        self.assertEqual(self.speak.quote("Bob's"), "'Bob''s'")

    def test_a_dollar_sign_is_not_expanded(self):
        self.assertEqual(self.speak.quote("$env:TEMP"), "'$env:TEMP'")

    def test_a_tilde_is_resolved_rather_than_passed_on(self):
        # PowerShell does not expand ~ inside a quoted string, and leaving it
        # unquoted is not an option for a path that may contain spaces.
        quoted = self.speak.quote("~/jobs")
        self.assertNotIn("~", quoted)
        self.assertIn(os.path.basename(os.path.expanduser("~")), quoted)

    def test_it_chains_with_a_semicolon_not_double_ampersand(self):
        # Windows PowerShell 5.1 has no pipeline chain operators; && is a
        # parser error there, not a no-op.
        command = self.speak.run_in("C:/jobs", "whoami")
        self.assertNotIn("&&", command)
        self.assertIn(";", command)


@unittest.skipIf(POWERSHELL is None, "no PowerShell on this machine")
class TestThePowerShellCommandsReallyWork(unittest.TestCase):
    """Generated PowerShell that is never executed proves nothing."""

    speak = dialect.POWERSHELL

    def setUp(self):
        raw = tempfile.mkdtemp(prefix="dialect_ps_")
        self.addCleanup(shutil.rmtree, raw, ignore_errors=True)
        # Through realpath: TEMP can be an 8.3 short path -- GitHub's Windows
        # runners hand out c:\users\runner~1\... -- while PowerShell reports
        # the long form back, so comparing the two spellings of one directory
        # failed on a difference that is not one.
        self.tmp = os.path.realpath(raw)

    def run_ps(self, command: str) -> str:
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_mkdirs_creates_parents_and_is_silent(self):
        target = os.path.join(self.tmp, "a", "b", "c")

        output = self.run_ps(self.speak.mkdirs(target))

        self.assertTrue(os.path.isdir(target))
        # Out-Null, or the created object is printed into whatever is parsing.
        self.assertEqual(output.strip(), "")

    def test_mkdirs_on_an_existing_directory_is_not_an_error(self):
        self.run_ps(self.speak.mkdirs(self.tmp))  # would raise on non-zero rc

    def test_a_directory_with_a_space_and_a_quote(self):
        target = os.path.join(self.tmp, "Bob's jobs")

        self.run_ps(self.speak.mkdirs(target))

        self.assertTrue(os.path.isdir(target))

    def test_read_files_returns_each_file_between_the_marks(self):
        first = os.path.join(self.tmp, "one")
        second = os.path.join(self.tmp, "two")
        with open(first, "w", encoding="ascii") as handle:
            handle.write("0\n")
        with open(second, "w", encoding="ascii") as handle:
            handle.write("7\n")

        output = self.run_ps(self.speak.read_files([first, second], "@@MARK@@"))

        chunks = output.split("@@MARK@@")[1:]
        self.assertEqual([chunk.strip() for chunk in chunks], ["0", "7"])

    def test_a_missing_file_reads_as_MISSING(self):
        # The poller's classification depends on this exact word: it is how a
        # job that was killed is told apart from one that finished.
        output = self.run_ps(self.speak.read_files([os.path.join(self.tmp, "nope")], "@@MARK@@"))

        self.assertEqual(output.split("@@MARK@@")[1].strip(), "MISSING")

    def test_list_dir_names_files_and_marks_directories(self):
        open(os.path.join(self.tmp, "result.out"), "w").close()
        os.makedirs(os.path.join(self.tmp, "scratch"), exist_ok=True)

        names = self.run_ps(self.speak.list_dir(self.tmp)).split()

        self.assertIn("result.out", names)
        # The trailing slash is how the caller skips sub-directories.
        self.assertIn("scratch/", names)

    def test_listing_a_directory_that_is_not_there_is_not_an_error(self):
        self.assertEqual(self.run_ps(self.speak.list_dir(os.path.join(self.tmp, "gone"))), "")

    def test_tail_returns_the_last_lines(self):
        path = os.path.join(self.tmp, "job.log")
        with open(path, "w", encoding="ascii") as handle:
            handle.write("\n".join(str(n) for n in range(100)) + "\n")

        output = self.run_ps(self.speak.tail(path, 3))

        self.assertEqual(output.split(), ["97", "98", "99"])

    def test_tailing_a_log_that_does_not_exist_yet_is_not_an_error(self):
        # A job that has not started has no log, and asking must not look like
        # a transport failure.
        self.assertEqual(self.run_ps(self.speak.tail(os.path.join(self.tmp, "no.log"), 10)), "")

    def test_run_in_changes_the_working_directory(self):
        output = self.run_ps(self.speak.run_in(self.tmp, "(Get-Location).Path"))

        self.assertEqual(os.path.normcase(output.strip()), os.path.normcase(self.tmp))

    def test_the_probe_reports_the_machine_name(self):
        output = self.run_ps(self.speak.probe())

        lines = [line for line in output.splitlines() if line.strip()]
        self.assertEqual(lines[0].strip(), "moleditpy_ok")
        self.assertTrue(lines[-1].strip())


if __name__ == "__main__":
    unittest.main()
