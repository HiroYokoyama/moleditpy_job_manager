"""Copying a file from one job's directory into another's, on the same host.

This is what a structure/checkpoint relay actually does at submit time --
after structure_relay.py has decided what to copy and submit_dialog.py has
substituted the tag, ``dialect.relay_lines()`` turns that into script lines,
and ``Scheduler.build_script`` places them where they run: after whatever
gated the job's start and before the payload, so a file relayed from a job
that had not finished at submit time is there either way (see
``dialect.py``'s and ``schedulers/base.py``'s own docstrings for why).
"""

from __future__ import annotations

import unittest

from job_manager.dialect import PowerShellDialect, for_host
from job_manager.models import SCHEDULER_WINDOWS
from job_manager.runner import submit_job
from job_manager.schedulers.base import get_scheduler

from .fakes import FakeTransport, make_host, make_job, make_preset


class TestDialectRelayLines(unittest.TestCase):
    def setUp(self):
        self.host = make_host()
        self.dialect = for_host(self.host)

    def test_a_flat_filename_is_copied_with_no_extra_directory(self):
        lines = self.dialect.relay_lines("/old/dir", ["opt.chk"])
        self.assertEqual(len(lines), 1)
        self.assertIn("cp -f", lines[0])
        self.assertIn("/old/dir/opt.chk", lines[0])
        self.assertIn("opt.chk", lines[0])

    def test_every_filename_gets_its_own_copy_line(self):
        lines = self.dialect.relay_lines("/old", ["opt.chk", "opt.xyz"])
        self.assertEqual(sum(1 for line in lines if "cp -f" in line), 2)

    def test_a_nested_path_gets_its_directory_made_first(self):
        lines = self.dialect.relay_lines("/old", ["opt.res/opt.xyz"])
        self.assertTrue(any("mkdir -p" in line and "opt.res" in line for line in lines))
        copy_index = next(i for i, line in enumerate(lines) if "cp -f" in line)
        mkdir_index = next(i for i, line in enumerate(lines) if "mkdir -p" in line)
        self.assertLess(mkdir_index, copy_index)

    def test_a_failed_copy_fails_the_job(self):
        lines = self.dialect.relay_lines("/old", ["opt.chk"])
        self.assertIn("exit 1", lines[0])

    def test_no_filenames_means_no_lines(self):
        self.assertEqual(self.dialect.relay_lines("/old", []), [])

    def test_the_powershell_dialect_uses_copy_item(self):
        host = make_host(scheduler=SCHEDULER_WINDOWS)
        dialect = for_host(host)
        self.assertIsInstance(dialect, PowerShellDialect)
        lines = dialect.relay_lines("C:/old", ["opt.chk"])
        self.assertTrue(any("Copy-Item" in line for line in lines))

    def test_the_powershell_dialect_throws_rather_than_exits(self):
        # relay_lines runs inside the wrapper's own try{}; `exit` there does
        # not reliably reach `catch{}`, `throw` does. See dialect.py.
        host = make_host(scheduler=SCHEDULER_WINDOWS)
        dialect = for_host(host)
        lines = dialect.relay_lines("C:/old", ["opt.chk"])
        self.assertTrue(any("throw" in line for line in lines))
        self.assertFalse(any(line.strip().startswith("exit") for line in lines))


class TestSchedulerPlacesRelayLines(unittest.TestCase):
    def test_relay_lines_appear_before_the_payload(self):
        scheduler = get_scheduler("slurm")
        preset = make_preset(command_template="g16 {input} > {stem}.log")
        script = scheduler.build_script(
            "myjob",
            preset,
            "run.inp",
            "job.log",
            relay_lines=["cp -f /old/opt.chk opt.chk"],
        )
        lines = script.splitlines()
        relay_index = next(i for i, line in enumerate(lines) if "cp -f" in line)
        payload_index = next(i for i, line in enumerate(lines) if "g16" in line)
        self.assertLess(relay_index, payload_index)

    def test_relay_lines_appear_after_the_predecessor_wait(self):
        scheduler = get_scheduler("shell")
        preset = make_preset(command_template="g16 {input} > {stem}.log")
        script = scheduler.build_script(
            "myjob",
            preset,
            "run.inp",
            "job.log",
            run_after="12345",
            relay_lines=["cp -f /old/opt.chk opt.chk"],
        )
        lines = script.splitlines()
        wait_index = next(i for i, line in enumerate(lines) if "kill -0" in line)
        relay_index = next(i for i, line in enumerate(lines) if "cp -f" in line)
        self.assertLess(wait_index, relay_index)

    def test_no_relay_lines_means_nothing_extra_in_the_script(self):
        scheduler = get_scheduler("slurm")
        preset = make_preset(command_template="g16 {input} > {stem}.log")
        script = scheduler.build_script("myjob", preset, "run.inp", "job.log")
        self.assertNotIn("cp -f", script)

    def test_the_windows_scheduler_places_relay_lines_inside_the_try_block(self):
        scheduler = get_scheduler(SCHEDULER_WINDOWS)
        preset = make_preset(command_template="g16 {input} > {stem}.log")
        script = scheduler.build_script(
            "myjob",
            preset,
            "run.inp",
            "job.log",
            relay_lines=["Copy-Item -LiteralPath 'C:/old/opt.chk' -Destination 'opt.chk' -Force"],
        )
        lines = script.splitlines()
        try_index = next(i for i, line in enumerate(lines) if "try" in line.lower())
        relay_index = next(i for i, line in enumerate(lines) if "Copy-Item" in line)
        self.assertLess(try_index, relay_index)


class TestSubmitJobWiresRelayLinesIntoTheScript(unittest.TestCase):
    def setUp(self):
        self.host = make_host()
        self.transport = FakeTransport(self.host).when("sbatch", stdout="42\n")

    def test_the_relay_copy_ends_up_in_the_submitted_script(self):
        job = make_job(host_id=self.host.id)
        preset = make_preset(command_template="g16 {input} > {stem}.log")
        submit_job(
            self.transport,
            self.host,
            preset,
            job,
            [],
            relay_source_dir="/old/opt_dir",
            relay_filenames=["opt.chk"],
        )
        self.assertIn("cp -f", job.command)
        self.assertIn("opt.chk", job.command)
        self.assertEqual(job.remote_job_id, "42")

    def test_no_relay_requested_means_no_copy_in_the_script(self):
        job = make_job(host_id=self.host.id)
        preset = make_preset(command_template="g16 {input} > {stem}.log")
        submit_job(self.transport, self.host, preset, job, [])
        self.assertNotIn("cp -f", job.command)


if __name__ == "__main__":
    unittest.main()
