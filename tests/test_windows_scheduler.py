"""The Windows wrapper, executed by a real PowerShell.

Exit-code detection is the most safety-critical code in the plugin -- a job
recorded as a clean success when it failed is worse than no tracking at all --
and this is a second implementation of it in a second language. Reading the
generated script proves nothing; every claim here is made by running it.

Skipped where there is no PowerShell, which is every Linux CI runner unless
pwsh is installed. That is why the *quoting* and *shape* tests are separate and
unconditional: those still guard the script on machines that cannot run it.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from job_manager.models import SENTINEL_NAME, SubmitPreset
from job_manager.schedulers import get_scheduler
from job_manager.schedulers.windows import ps_quote

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")

#: Running the wrapper needs Windows, not merely a PowerShell. GitHub's Ubuntu
#: images ship pwsh, so gating on the interpreter alone ran these on Linux --
#: where the payloads (cmd /c) and the cancel (taskkill) do not exist.
ON_WINDOWS = os.name == "nt" and POWERSHELL is not None


def make_preset(command: str, **kwargs) -> SubmitPreset:
    preset = SubmitPreset(command_template=command)
    for key, value in kwargs.items():
        setattr(preset, key, value)
    return preset


class TestQuoting(unittest.TestCase):
    """Unconditional: these guard the script where it cannot be run."""

    def test_a_plain_path_is_single_quoted(self):
        self.assertEqual(ps_quote(r"C:\jobs\opt"), r"'C:\jobs\opt'")

    def test_a_single_quote_is_doubled(self):
        self.assertEqual(ps_quote("Bob's jobs"), "'Bob''s jobs'")

    def test_a_dollar_sign_is_not_expanded(self):
        # Single quotes, not double: PowerShell expands $ and backticks inside
        # double quotes, and a path is not an expression to evaluate.
        quoted = ps_quote("$env:TEMP")
        self.assertTrue(quoted.startswith("'") and quoted.endswith("'"))
        self.assertIn("$env:TEMP", quoted)

    def test_a_backtick_survives_untouched(self):
        self.assertEqual(ps_quote("a`b"), "'a`b'")


class TestScriptShape(unittest.TestCase):
    scheduler = get_scheduler("windows")

    def script(self, command="cmd /c exit 0", **kwargs) -> str:
        return self.scheduler.build_script(
            "job", make_preset(command), "in.inp", "job.log", remote_dir=r"C:\jobs\opt", **kwargs
        )

    def test_it_writes_the_sentinel_from_a_finally_block(self):
        # try/finally is PowerShell's `trap ... EXIT`: it runs however the
        # script leaves, including when the payload calls exit itself.
        script = self.script()
        self.assertIn("} finally {", script)
        self.assertIn(SENTINEL_NAME, script.split("} finally {")[1])

    def test_the_sentinel_is_written_without_a_byte_order_mark(self):
        # Set-Content writes a BOM with most encodings on Windows PowerShell
        # 5.1, and a BOM in front of the exit code makes it unparseable.
        self.assertIn("-Encoding ascii", self.script())

    def test_the_job_directory_is_baked_in(self):
        self.assertIn(r"Set-Location -LiteralPath 'C:\jobs\opt'", self.script())
        self.assertNotIn("PSScriptRoot", self.script())

    def test_the_exit_code_is_defaulted_for_a_cmdlet(self):
        # $LASTEXITCODE is set by native executables only, and otherwise holds
        # the previous program's status.
        self.assertIn("if ($null -eq $__moleditpy_rc) { $__moleditpy_rc = 0 }", self.script())

    def test_it_uses_crlf(self):
        self.assertIn("\r\n", self.script())

    def test_a_start_time_holds_the_job(self):
        script = self.script(start_after=1786000000)
        self.assertIn("FromUnixTimeSeconds(1786000000)", script)

    def test_a_chained_job_waits_for_the_process(self):
        script = self.script(run_after="4242")
        self.assertIn("Get-Process -Id 4242", script)

    def test_a_non_numeric_predecessor_is_never_interpolated(self):
        self.assertNotIn("Get-Process", self.script(run_after="1; rm -rf /"))


class TestCommands(unittest.TestCase):
    scheduler = get_scheduler("windows")

    def test_submit_redirects_the_streams_to_different_files(self):
        # PowerShell refuses to send stdout and stderr to one file.
        command = self.scheduler.submit_command("moleditpy_run.ps1", "job.log")
        self.assertIn("-RedirectStandardOutput (Join-Path $d 'job.log')", command)
        self.assertIn("-RedirectStandardError (Join-Path $d 'job.log.err')", command)

    def test_submit_makes_every_path_absolute(self):
        # Start-Process resolves a relative path against PowerShell's location
        # in pwsh 7 and against the process working directory in 5.1, and the
        # caller reached this directory with Set-Location -- so neither reading
        # may be relied on.
        command = self.scheduler.submit_command("moleditpy_run.ps1", "job.log")
        self.assertIn("$d = (Get-Location).Path", command)
        self.assertIn("(Join-Path $d 'moleditpy_run.ps1')", command)
        self.assertIn("-WorkingDirectory $d", command)

    def test_submit_prints_the_process_id(self):
        self.assertTrue(self.scheduler.submit_command("run.ps1", "job.log").endswith("$p.Id"))

    def test_the_process_id_is_read_back(self):
        self.assertEqual(self.scheduler.parse_submit_output("\n4242\n", ""), "4242")

    def test_a_status_query_covers_every_job_at_once(self):
        command = self.scheduler.status_command("me", ["1", "2", "3"])
        self.assertIn("Get-Process -Id 1,2,3", command)

    def test_nothing_tracked_asks_nothing(self):
        self.assertEqual(self.scheduler.status_command("me", []), "exit 0")

    def test_cancel_kills_the_whole_tree(self):
        # Stop-Process alone orphans the payload: the wrapper is a PowerShell
        # host that launched the real program as a child.
        self.assertEqual(self.scheduler.cancel_command("4242"), "taskkill /PID 4242 /T /F")

    def test_a_non_numeric_job_id_is_never_run(self):
        # A job list can come from anywhere, and this string is executed.
        self.assertEqual(self.scheduler.cancel_command("1 & calc.exe"), "exit 0")


@unittest.skipUnless(ON_WINDOWS, "the Windows wrapper needs Windows, not just a PowerShell")
class TestItReallyRuns(unittest.TestCase):
    """The part that cannot be proved by reading."""

    scheduler = get_scheduler("windows")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="windows_sched_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_wrapper(self, command: str, **kwargs) -> str:
        script = self.scheduler.build_script(
            "job", make_preset(command), "in.inp", "job.log", remote_dir=self.tmp, **kwargs
        )
        path = os.path.join(self.tmp, "moleditpy_run.ps1")
        with open(path, "w", encoding="ascii", newline="") as handle:
            handle.write(script)
        subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        sentinel = os.path.join(self.tmp, SENTINEL_NAME)
        if not os.path.exists(sentinel):
            return "MISSING"
        with open(sentinel, "rb") as handle:
            raw = handle.read()
        # Read as bytes on purpose: a BOM here is the bug being guarded against.
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "the sentinel has a UTF-8 BOM")
        return raw.decode("ascii").strip()

    def test_a_successful_job_records_zero(self):
        self.assertEqual(self.run_wrapper("cmd /c exit 0"), "0")

    def test_a_failing_job_records_its_real_code(self):
        self.assertEqual(self.run_wrapper("cmd /c exit 7"), "7")

    def test_a_payload_that_exits_the_script_still_records_its_code(self):
        # A command template ending in `exit` leaves the try block before the
        # exit code is captured. Found by running it: the sentinel was written
        # (the finally does run) but held the placeholder 1.
        self.assertEqual(self.run_wrapper("cmd /c exit 3\r\nexit 3"), "3")

    def test_a_successful_payload_that_exits_is_not_recorded_as_a_failure(self):
        # The dangerous direction of the same bug: this recorded 1, so a job
        # that succeeded was reported FAILED.
        self.assertEqual(self.run_wrapper("cmd /c exit 0\r\nexit 0"), "0")

    def test_a_cmdlet_payload_does_not_inherit_an_earlier_status(self):
        # $LASTEXITCODE would otherwise still hold the previous program's code.
        self.assertEqual(self.run_wrapper("Write-Output 'hello'"), "0")

    def test_a_redirect_in_the_command_template_writes_plain_text(self):
        # `>` is Out-File in Windows PowerShell 5.1, whose default encoding is
        # UTF-16 with a BOM -- so `orca in.inp > out.out`, the shape every
        # built-in template has, wrote an output file no parser can read.
        target = os.path.join(self.tmp, "out.out").replace("\\", "/")
        self.run_wrapper(f"cmd /c echo SCF_DONE > '{target}'")

        with open(os.path.join(self.tmp, "out.out"), "rb") as handle:
            raw = handle.read()

        self.assertFalse(raw.startswith(b"\xff\xfe"), "the output file is UTF-16")
        self.assertIn(b"SCF_DONE", raw)

    def test_the_sentinel_parses_as_an_integer(self):
        # What the poller actually does with it.
        self.assertEqual(int(self.run_wrapper("cmd /c exit 42")), 42)

    def test_a_missing_program_is_a_failure_not_a_success(self):
        outcome = self.run_wrapper("cmd /c no_such_program_xyz")
        self.assertNotEqual(outcome, "0")
        self.assertNotEqual(outcome, "MISSING")

    def test_a_throwing_pre_command_still_reaches_the_finally(self):
        script_preset = make_preset("cmd /c exit 0", pre_commands=["throw 'boom'"])
        script = self.scheduler.build_script(
            "job", script_preset, "in.inp", "job.log", remote_dir=self.tmp
        )
        path = os.path.join(self.tmp, "moleditpy_run.ps1")
        with open(path, "w", encoding="ascii", newline="") as handle:
            handle.write(script)
        subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertTrue(os.path.exists(os.path.join(self.tmp, SENTINEL_NAME)))

    def test_a_path_with_a_space_and_a_quote_is_handled(self):
        awkward = os.path.join(self.tmp, "Bob's jobs")
        os.makedirs(awkward, exist_ok=True)
        script = self.scheduler.build_script(
            "job", make_preset("cmd /c exit 5"), "in.inp", "job.log", remote_dir=awkward
        )
        path = os.path.join(self.tmp, "moleditpy_run.ps1")
        with open(path, "w", encoding="ascii", newline="") as handle:
            handle.write(script)
        subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True,
            text=True,
            timeout=120,
        )

        with open(os.path.join(awkward, SENTINEL_NAME), encoding="ascii") as handle:
            self.assertEqual(handle.read().strip(), "5")


if __name__ == "__main__":
    unittest.main()
