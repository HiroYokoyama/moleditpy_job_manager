import os
import shutil
import tempfile
import unittest

from job_manager import runner
from job_manager.models import (
    SENTINEL_NAME,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_LOST,
    STATE_PENDING,
    STATE_RUNNING,
    STATE_SUBMITTED,
    Job,
)
from job_manager.transport.base import TransportError

from .fakes import FakeTransport, make_host, make_job, make_preset


def _write(directory, name, text="data"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class TestRemoteDir(unittest.TestCase):
    def test_uses_the_host_root_and_a_timestamp(self):
        host = make_host(remote_root="/scratch/me")
        path = runner.make_remote_dir(host, "my job", when=0)
        self.assertTrue(path.startswith("/scratch/me/"))
        self.assertTrue(path.endswith("_my_job"))

    def test_name_cannot_escape_the_root(self):
        path = runner.make_remote_dir(make_host(), "../../etc", when=0)
        self.assertNotIn("..", path)


class TestShortId(unittest.TestCase):
    def test_strips_the_host_suffix(self):
        self.assertEqual(runner.short_id("58231.head.cluster"), "58231")

    def test_plain_id_is_unchanged(self):
        self.assertEqual(runner.short_id("58231"), "58231")

    def test_empty(self):
        self.assertEqual(runner.short_id(""), "")


class TestSubmit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="submit_")
        self.input_path = _write(self.tmp, "mol.inp", "! B3LYP\n")
        self.host = make_host()
        self.preset = make_preset()
        self.transport = FakeTransport(self.host).when("sbatch", stdout="4823917\n")

    def submit(self, job=None, files=None):
        job = job or Job(name="mol")
        if files is None:
            files = [self.input_path]
        return runner.submit_job(self.transport, self.host, self.preset, job, files)

    def test_returns_the_queue_id(self):
        self.assertEqual(self.submit().remote_job_id, "4823917")

    def test_the_uploaded_script_cds_into_this_job_s_own_directory(self):
        # sbatch runs a copy of the script from its spool directory, so the
        # script cannot work out where its input is from $0.
        job = self.submit()
        script = self.transport.uploaded_text[f"{job.remote_dir}/moleditpy_run.sh"]
        self.assertIn(f"cd {job.remote_dir} || exit 1", script)
        self.assertNotIn('dirname "$0"', script)

    def test_state_and_timestamp(self):
        job = self.submit()
        self.assertEqual(job.state, STATE_SUBMITTED)
        self.assertGreater(job.submitted_at, 0)

    def test_creates_the_remote_directory_first(self):
        self.submit()
        self.assertTrue(self.transport.commands[0].startswith("mkdir -p"))

    def test_uploads_the_input_and_the_script(self):
        job = self.submit()
        remote_names = [os.path.basename(remote) for _, remote in self.transport.uploads]
        self.assertIn("mol.inp", remote_names)
        self.assertIn("moleditpy_run.sh", remote_names)
        self.assertIn(job.remote_dir, self.transport.uploads[0][1])

    def test_uploaded_script_uses_unix_line_endings(self):
        # Written from Windows; CRLF would make the remote shebang unusable.
        self.submit()
        captured = [t for r, t in self.transport.uploaded_text.items() if r.endswith(".sh")][0]
        self.assertNotIn("\r\n", captured)
        self.assertIn(SENTINEL_NAME, captured)

    def test_the_temp_script_is_cleaned_up(self):
        self.submit()
        local_script = [
            local for local, remote in self.transport.uploads if remote.endswith(".sh")
        ][0]
        self.assertFalse(os.path.exists(local_script))

    def test_submits_from_inside_the_job_directory(self):
        job = self.submit()
        submit_cmd = [c for c in self.transport.commands if "sbatch" in c][0]
        self.assertTrue(submit_cmd.startswith("cd "))
        self.assertIn(job.remote_dir.rsplit("/", 1)[-1], submit_cmd)

    def test_tilde_in_the_remote_root_stays_expandable(self):
        # shlex.quote'ing the whole path would make the shell look for a
        # directory literally named '~'.
        self.submit()
        mkdir_cmd = self.transport.commands[0]
        self.assertTrue(mkdir_cmd.startswith("mkdir -p ~/"))

    def test_multiple_inputs_are_all_uploaded(self):
        second = _write(self.tmp, "extra.xyz")
        self.submit(files=[self.input_path, second])
        names = [os.path.basename(remote) for _, remote in self.transport.uploads]
        self.assertIn("extra.xyz", names)

    def test_first_file_is_the_command_input(self):
        second = _write(self.tmp, "extra.xyz")
        job = self.submit(files=[self.input_path, second])
        self.assertIn("orca mol.inp", job.command)

    def test_no_files_is_allowed_but_no_command_is_not(self):
        # A command-only job is a real job; a job with nothing to run is not.
        # See tests/test_command_only.py for what the former does.
        self.submit(files=[])
        with self.assertRaises(ValueError):
            runner.submit_job(
                self.transport,
                self.host,
                make_preset(command_template="  "),
                Job(name="mol"),
                [],
            )

    def test_submit_failure_raises(self):
        self.transport.clear_rules()
        self.transport.when("sbatch", rc=1, stderr="Invalid account")
        with self.assertRaises(TransportError) as caught:
            self.submit()
        self.assertIn("Invalid account", str(caught.exception))

    def test_unparseable_job_id_raises(self):
        self.transport.clear_rules()
        self.transport.when("sbatch", stdout="something unexpected\n")
        with self.assertRaises(TransportError) as caught:
            self.submit()
        self.assertIn("job id", str(caught.exception))

    def test_existing_remote_dir_is_respected(self):
        job = Job(name="mol", remote_dir="/scratch/preset")
        self.assertEqual(self.submit(job=job).remote_dir, "/scratch/preset")


class TestPollHost(unittest.TestCase):
    def setUp(self):
        self.host = make_host()
        self.transport = FakeTransport(self.host)

    def test_single_status_call_covers_every_job(self):
        jobs = [make_job(id=f"j{i}", remote_job_id=str(100 + i)) for i in range(5)]
        self.transport.when(
            "squeue", stdout="100 RUNNING\n101 RUNNING\n102 RUNNING\n103 RUNNING\n104 RUNNING\n"
        )
        runner.poll_host(self.transport, self.host, jobs)
        self.assertEqual(self.transport.count_matching("squeue"), 1)

    def test_state_change_is_reported(self):
        job = make_job(state=STATE_PENDING, remote_job_id="100")
        self.transport.when("squeue", stdout="100 RUNNING\n")
        self.assertEqual(
            runner.poll_host(self.transport, self.host, [job]), {job.id: STATE_RUNNING}
        )

    def test_unchanged_state_is_omitted(self):
        job = make_job(state=STATE_RUNNING, remote_job_id="100")
        self.transport.when("squeue", stdout="100 RUNNING\n")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {})

    def test_jobs_without_a_queue_id_are_skipped(self):
        job = make_job(remote_job_id="")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {})
        self.assertEqual(self.transport.commands, [])

    def test_gone_from_queue_with_rc_zero_is_done(self):
        job = make_job(state=STATE_RUNNING)
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n0\n")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {job.id: STATE_DONE})
        self.assertEqual(job.rc, 0)

    def test_gone_from_queue_with_nonzero_rc_is_failed(self):
        job = make_job(state=STATE_RUNNING)
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n1\n")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {job.id: STATE_FAILED})
        self.assertEqual(job.rc, 1)

    def test_gone_from_queue_without_a_sentinel_is_lost(self):
        job = make_job(state=STATE_RUNNING)
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\nMISSING\n")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {job.id: STATE_LOST})

    def test_a_cancelled_job_stays_cancelled(self):
        job = make_job(state=STATE_CANCELLED)
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\nMISSING\n")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {})

    def test_garbage_in_the_sentinel_is_treated_as_lost(self):
        job = make_job(state=STATE_RUNNING)
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\nnot-a-number\n")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {job.id: STATE_LOST})

    def test_one_sentinel_sweep_for_several_finished_jobs(self):
        jobs = [
            make_job(id="a", remote_job_id="1", remote_dir="/d/a", state=STATE_RUNNING),
            make_job(id="b", remote_job_id="2", remote_dir="/d/b", state=STATE_RUNNING),
        ]
        self.transport.when("squeue", stdout="")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n0\n@@MOLEDITPY@@\n2\n")
        updates = runner.poll_host(self.transport, self.host, jobs)
        self.assertEqual(updates, {"a": STATE_DONE, "b": STATE_FAILED})
        self.assertEqual(self.transport.count_matching(SENTINEL_NAME), 1)

    def test_mixed_queued_and_finished(self):
        queued = make_job(id="q", remote_job_id="1", state=STATE_PENDING)
        gone = make_job(id="g", remote_job_id="2", state=STATE_RUNNING)
        self.transport.when("squeue", stdout="1 RUNNING\n")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n0\n")
        updates = runner.poll_host(self.transport, self.host, [queued, gone])
        self.assertEqual(updates, {"q": STATE_RUNNING, "g": STATE_DONE})

    def test_pbs_truncated_id_still_matches(self):
        host = make_host(scheduler="pbs")
        job = make_job(remote_job_id="58231.headnode.cluster", state=STATE_PENDING)
        transport = FakeTransport(host).when(
            "qstat", stdout="58231.hea alice batch j 1 1 1 -- 10:00 R 00:12\n"
        )
        self.assertEqual(runner.poll_host(transport, host, [job]), {job.id: STATE_RUNNING})

    def test_status_failure_with_output_is_not_fatal(self):
        job = make_job(state=STATE_RUNNING)
        self.transport.when("squeue", rc=1, stdout="100 RUNNING\n", stderr="warning")
        runner.poll_host(self.transport, self.host, [job])

    def test_status_failure_without_output_raises(self):
        job = make_job()
        self.transport.when("squeue", rc=2, stderr="squeue: error: Invalid user")
        with self.assertRaises(TransportError):
            runner.poll_host(self.transport, self.host, [job])

    def test_empty_queue_error_is_tolerated(self):
        job = make_job(state=STATE_RUNNING)
        self.transport.when("squeue", rc=1, stderr="Unknown Job Id")
        self.transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n0\n")
        self.assertEqual(runner.poll_host(self.transport, self.host, [job]), {job.id: STATE_DONE})


class TestFileSelection(unittest.TestCase):
    def test_matches_globs(self):
        names = ["a.out", "b.log", "c.tmp", "d.xyz"]
        self.assertEqual(runner.select_files(names, ["*.out", "*.xyz"]), ["a.out", "d.xyz"])

    def test_no_patterns_selects_everything(self):
        self.assertEqual(runner.select_files(["a", "b"], []), ["a", "b"])

    def test_blank_patterns_are_ignored(self):
        self.assertEqual(runner.select_files(["a.out"], ["  ", "*.out"]), ["a.out"])

    def test_exact_name_matches(self):
        self.assertEqual(runner.select_files(["job.log", "x"], ["job.log"]), ["job.log"])

    def test_list_remote_files_skips_directories(self):
        transport = FakeTransport().when("ls -p", stdout="a.out\nsub/\nb.log\n\n")
        self.assertEqual(runner.list_remote_files(transport, "/d"), ["a.out", "b.log"])


class TestFetchResults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fetch_")
        # rc=0: a job that finished cleanly, which is the only case where the
        # wrapper's own log is dropped. A failure keeps it.
        self.job = make_job(fetch_globs=["*.out"], rc=0)
        self.transport = FakeTransport().when("ls -p", stdout="mol.out\nmol.gbw\njob.log\n")

    def test_downloads_matching_files(self):
        paths = runner.fetch_results(self.transport, self.job, self.tmp)
        names = sorted(os.path.basename(p) for p in paths)
        self.assertEqual(names, ["mol.out"])

    def test_the_wrappers_own_log_is_not_a_result(self):
        # job.log holds whatever the command wrote to stdout and stderr; the
        # calculation's real output is the file the command was told to write.
        # It used to be forced into every download, so a directory of results
        # carried a job.log next to the .out nobody wanted to tell apart.
        paths = runner.fetch_results(self.transport, self.job, self.tmp, globs=["*.out"])
        self.assertFalse(any(p.endswith("job.log") for p in paths))

    def test_a_wildcard_that_covers_it_does_not_bring_it_back(self):
        # *.log is in the default patterns for Gaussian's output, which is a
        # different file entirely.
        paths = runner.fetch_results(self.transport, self.job, self.tmp, globs=["*.log"])
        self.assertEqual([os.path.basename(p) for p in paths], [])

    def test_naming_it_exactly_still_fetches_it(self):
        # An explicit request, unlike a wildcard that happens to cover it.
        paths = runner.fetch_results(self.transport, self.job, self.tmp, globs=["job.log"])
        self.assertEqual([os.path.basename(p) for p in paths], ["job.log"])

    def test_files_land_in_the_target_directory(self):
        paths = runner.fetch_results(self.transport, self.job, self.tmp)
        self.assertTrue(all(p.startswith(self.tmp) for p in paths))
        self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_creates_the_local_directory(self):
        target = os.path.join(self.tmp, "new", "dir")
        runner.fetch_results(self.transport, self.job, target)
        self.assertTrue(os.path.isdir(target))

    def test_one_failed_download_does_not_abort_the_rest(self):
        self.transport.fail_downloads = ("mol.out",)
        paths = runner.fetch_results(self.transport, self.job, self.tmp, globs=["*.out", "*.gbw"])
        self.assertEqual([os.path.basename(p) for p in paths], ["mol.gbw"])

    def test_nothing_matching_returns_empty(self):
        job = make_job(fetch_globs=["*.nothing"], log_file="")
        self.assertEqual(runner.fetch_results(self.transport, job, self.tmp), [])

    def test_no_patterns_at_all_means_fetch_everything(self):
        # Clearing the Fetch patterns field is "no filter", not "the log only".
        # Appending the log to an empty list turned it into the latter.
        job = make_job(fetch_globs=[])
        names = sorted(
            os.path.basename(p) for p in runner.fetch_results(self.transport, job, self.tmp)
        )
        self.assertEqual(names, ["job.log", "mol.gbw", "mol.out"])

    def test_blank_patterns_count_as_no_patterns(self):
        job = make_job(fetch_globs=["", "  "])
        paths = runner.fetch_results(self.transport, job, self.tmp)
        self.assertEqual(len(paths), 3)


class TestCancel(unittest.TestCase):
    def setUp(self):
        self.host = make_host()
        self.transport = FakeTransport(self.host)

    def test_issues_the_cancel_command(self):
        runner.cancel_job(self.transport, self.host, make_job(remote_job_id="55"))
        self.assertTrue(self.transport.ran("scancel 55"))

    def test_no_queue_id_is_a_no_op(self):
        runner.cancel_job(self.transport, self.host, make_job(remote_job_id=""))
        self.assertEqual(self.transport.commands, [])

    def test_already_gone_is_not_an_error(self):
        self.transport.when("scancel", rc=1, stderr="scancel: error: Invalid job id 55")
        runner.cancel_job(self.transport, self.host, make_job(remote_job_id="55"))

    def test_other_failures_raise(self):
        self.transport.when("scancel", rc=1, stderr="Access denied")
        with self.assertRaises(TransportError):
            runner.cancel_job(self.transport, self.host, make_job(remote_job_id="55"))


class TestTailLog(unittest.TestCase):
    def test_returns_the_output(self):
        transport = FakeTransport().when("tail", stdout="last lines\n")
        self.assertEqual(runner.tail_log(transport, make_job()), "last lines\n")

    def test_requests_the_configured_number_of_lines(self):
        transport = FakeTransport()
        runner.tail_log(transport, make_job(), lines=50)
        self.assertTrue(transport.ran("tail -n 50"))

    def test_no_log_file_returns_empty(self):
        transport = FakeTransport()
        self.assertEqual(runner.tail_log(transport, make_job(log_file="")), "")
        self.assertEqual(transport.commands, [])


class TestRoundTrip(unittest.TestCase):
    """Submit -> pending -> running -> gone -> sentinel -> download."""

    def test_full_cycle(self):
        tmp = tempfile.mkdtemp(prefix="roundtrip_")
        input_path = _write(tmp, "mol.inp")
        host = make_host()
        preset = make_preset(fetch_globs=["*.out"])
        transport = FakeTransport(host).when("sbatch", stdout="777\n")

        job = runner.submit_job(transport, host, preset, Job(name="mol"), [input_path])
        job.fetch_globs = list(preset.fetch_globs)
        self.assertEqual(job.state, STATE_SUBMITTED)

        transport.clear_rules()
        transport.when("squeue", stdout="777 PENDING\n")
        self.assertEqual(runner.poll_host(transport, host, [job])[job.id], STATE_PENDING)
        job.touch(STATE_PENDING)

        transport.clear_rules()
        transport.when("squeue", stdout="777 RUNNING\n")
        self.assertEqual(runner.poll_host(transport, host, [job])[job.id], STATE_RUNNING)
        job.touch(STATE_RUNNING)

        transport.clear_rules()
        transport.when("squeue", stdout="")
        transport.when(SENTINEL_NAME, stdout="@@MOLEDITPY@@\n0\n")
        self.assertEqual(runner.poll_host(transport, host, [job])[job.id], STATE_DONE)
        job.touch(STATE_DONE)

        transport.clear_rules()
        transport.when("ls -p", stdout="mol.out\njob.log\nmol.tmp\n")
        downloaded = runner.fetch_results(transport, job, os.path.join(tmp, "results"))
        # The wrapper's own log stays on the host; the calculation's output is
        # what comes back.
        self.assertEqual(sorted(os.path.basename(p) for p in downloaded), ["mol.out"])


class TestAnInputIsNeverOverwritten(unittest.TestCase):
    """Results land beside the input, so the input is in the target directory.

    A fetch glob of ``*.xyz`` against an input named ``mol.xyz`` would write
    the remote copy back over the user's own file. The same bytes today -- but
    a truncated download would destroy the original.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="overwrite_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = os.path.join(self.tmp, "mol.xyz")
        with open(self.input, "w", encoding="utf-8") as handle:
            handle.write("ORIGINAL")
        self.transport = FakeTransport(make_host())
        self.transport.when("ls -p -1", stdout="mol.xyz\nmol.out\n")

    def _job(self) -> Job:
        return Job(
            name="opt",
            remote_dir="/remote/opt",
            input_files=[self.input],
            fetch_globs=["*.xyz", "*.out"],
        )

    def test_the_input_file_is_not_downloaded_over(self):
        runner.fetch_results(self.transport, self._job(), self.tmp)

        with open(self.input, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ORIGINAL")

    def test_it_is_not_even_requested(self):
        runner.fetch_results(self.transport, self._job(), self.tmp)

        requested = [remote for remote, _local in self.transport.downloads]
        self.assertNotIn("/remote/opt/mol.xyz", requested)

    def test_everything_else_still_comes_back(self):
        downloaded = runner.fetch_results(self.transport, self._job(), self.tmp)

        self.assertEqual([os.path.basename(p) for p in downloaded], ["mol.out"])

    def test_the_same_name_elsewhere_is_downloaded_normally(self):
        # Only the job's *own* input is protected, not every file called that.
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)

        downloaded = runner.fetch_results(self.transport, self._job(), other)

        self.assertEqual(sorted(os.path.basename(p) for p in downloaded), ["mol.out", "mol.xyz"])


if __name__ == "__main__":
    unittest.main()
