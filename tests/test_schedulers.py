import unittest

from job_manager.models import (
    SENTINEL_NAME,
    STATE_COMPLETING,
    STATE_PENDING,
    STATE_RUNNING,
    SubmitPreset,
)
from job_manager.schedulers import (
    STATE_UNKNOWN,
    available_schedulers,
    format_command,
    get_scheduler,
)


class TestRegistry(unittest.TestCase):
    def test_every_backend_is_registered(self):
        names = {s.name for s in available_schedulers()}
        self.assertEqual(names, {"slurm", "pbs", "sge", "shell", "windows"})

    def test_unknown_scheduler_raises(self):
        with self.assertRaises(ValueError):
            get_scheduler("condor")

    def test_every_scheduler_has_a_label(self):
        self.assertTrue(all(s.label for s in available_schedulers()))

    def test_the_built_in_modes_lead_the_list(self):
        # The two that need nothing installed on the far end come first, and a
        # new host defaults to the first of them.
        self.assertEqual([s.name for s in available_schedulers()][:2], ["shell", "windows"])
        self.assertEqual([s.name for s in available_schedulers()][2:], ["slurm", "pbs", "sge"])

    def test_a_new_host_uses_the_built_in_mode(self):
        from job_manager.models import SCHEDULER_SHELL, HostProfile

        self.assertEqual(HostProfile().scheduler, SCHEDULER_SHELL)


class TestFormatCommand(unittest.TestCase):
    def setUp(self):
        self.preset = SubmitPreset(cpus_per_task=8, memory="16G", queue="short")

    def test_input_and_stem(self):
        result = format_command("orca {input} > {stem}.out", "mol.inp", self.preset)
        self.assertEqual(result, "orca mol.inp > mol.out")

    def test_resource_placeholders(self):
        result = format_command("mpirun -np {cpus} x {memory}", "a.inp", self.preset)
        self.assertEqual(result, "mpirun -np 8 x 16G")

    def test_unknown_placeholder_is_left_alone_instead_of_crashing(self):
        template = "run {nonsense}"
        self.assertEqual(format_command(template, "a.inp", self.preset), template)

    def test_empty_template(self):
        self.assertEqual(format_command("", "a.inp", self.preset), "")

    def test_stem_of_a_dotted_name(self):
        result = format_command("{stem}", "job.step1.inp", self.preset)
        self.assertEqual(result, "job.step1")


class ScriptContractMixin:
    """Every scheduler's script must obey the same completion contract."""

    scheduler_name = ""

    def build(self, **preset_kwargs):
        preset = SubmitPreset(command_template="run {input}", **preset_kwargs)
        return get_scheduler(self.scheduler_name).build_script(
            "my job", preset, "mol.inp", "job.log"
        )

    def test_starts_with_a_shebang(self):
        self.assertTrue(self.build().startswith("#!/bin/bash"))

    def test_the_sentinel_is_written_from_an_exit_trap(self):
        # A trailing echo would be skipped by a payload that calls exit itself,
        # and the job would be reported LOST instead of FAILED.
        script = self.build()
        self.assertIn(
            f"""trap '__moleditpy_rc=$?; echo "$__moleditpy_rc" > {SENTINEL_NAME}.tmp"""
            f""" && mv -f {SENTINEL_NAME}.tmp {SENTINEL_NAME}' EXIT""",
            script,
        )

    def test_the_sentinel_is_never_written_in_place(self):
        # `>` truncates before it writes, and the reading side cannot tell an
        # empty sentinel from a missing one: it would call a finished job LOST.
        script = self.build()
        self.assertNotIn(f'"$__moleditpy_rc" > {SENTINEL_NAME}\'', script)
        self.assertIn(f"mv -f {SENTINEL_NAME}.tmp {SENTINEL_NAME}", script)

    def test_the_trap_is_armed_before_the_payload_runs(self):
        script = self.build()
        self.assertLess(script.index("trap '__moleditpy_rc"), script.index("run mol.inp"))

    def test_the_payload_is_the_last_thing_in_the_script(self):
        lines = [line for line in self.build().splitlines() if line.strip()]
        self.assertEqual(lines[-1], "run mol.inp")

    def test_stale_sentinel_is_removed_before_the_run(self):
        script = self.build()
        self.assertLess(
            script.index(f"rm -f {SENTINEL_NAME}"),
            script.index("run mol.inp"),
        )

    def test_the_stale_sentinel_goes_before_the_trap_is_installed(self):
        # Otherwise there is a window in which the trap could fire with the
        # previous run's exit code still on disk, and a job killed in it would
        # be reported as having finished with that earlier attempt's status.
        script = self.build()
        self.assertLess(
            script.index(f"rm -f {SENTINEL_NAME}"),
            script.index("trap '__moleditpy_rc"),
        )

    def test_the_wrapper_removes_nothing_else(self):
        # The job directory holds the user's inputs and outputs. The sentinel
        # is the only thing in it the plugin is entitled to delete.
        removals = [
            line.strip()
            for line in self.build().splitlines()
            if line.strip().startswith(("rm ", "rm -"))
        ]
        self.assertEqual(removals, [f"rm -f {SENTINEL_NAME}"])

    def test_job_name_is_sanitized_into_the_directives(self):
        # A raw name with a space would break every directive syntax.
        self.assertNotIn("my job", self.build())
        self.assertIn("my_job", self.build())

    def test_modules_and_pre_commands_run_before_the_payload(self):
        script = self.build(modules=["orca/5"], pre_commands=["export X=1"])
        self.assertLess(script.index("module load orca/5"), script.index("run mol.inp"))
        self.assertLess(script.index("export X=1"), script.index("run mol.inp"))

    def test_extra_directives_are_included(self):
        self.assertIn("#custom directive", self.build(extra_directives=["#custom directive"]))

    def test_blank_modules_are_skipped(self):
        self.assertNotIn("module load \n", self.build(modules=["", "  "]))

    # --- working directory --------------------------------------------------

    def build_in(self, remote_dir):
        return get_scheduler(self.scheduler_name).build_script(
            "my job",
            SubmitPreset(command_template="run {input}"),
            "mol.inp",
            "job.log",
            remote_dir=remote_dir,
        )

    def test_it_cds_into_the_job_directory_it_was_given(self):
        # sbatch and qsub run a *copy* of the script from their own spool
        # directory, so `dirname "$0"` is that spool dir and not the directory
        # holding the uploaded input: the payload could not find its input, and
        # the sentinel landed somewhere the poller never looks (reported LOST).
        script = self.build_in("~/moleditpy_jobs/20260101_my_job")
        self.assertIn("cd ~/moleditpy_jobs/20260101_my_job || exit 1", script)
        self.assertNotIn('dirname "$0"', script)

    def test_a_job_directory_with_a_space_is_quoted(self):
        self.assertIn(
            "cd '/scratch/my jobs/run 1' || exit 1", self.build_in("/scratch/my jobs/run 1")
        )

    def test_the_cd_comes_before_the_sentinel_and_the_payload(self):
        script = self.build_in("/scratch/j")
        self.assertLess(script.index("cd /scratch/j"), script.index(f"rm -f {SENTINEL_NAME}"))
        self.assertLess(script.index("cd /scratch/j"), script.index("run mol.inp"))

    def test_without_a_job_directory_it_falls_back_to_the_queue_s_own_variable(self):
        # Only the wizard's preview builds a script before the directory
        # exists. Each queue exports where the job was submitted from.
        script = self.build()
        self.assertIn("SLURM_SUBMIT_DIR", script)
        self.assertIn("PBS_O_WORKDIR", script)


class TestSlurm(ScriptContractMixin, unittest.TestCase):
    scheduler_name = "slurm"

    def setUp(self):
        self.scheduler = get_scheduler("slurm")

    def test_directives(self):
        preset = SubmitPreset(
            walltime="02:00:00",
            nodes=2,
            ntasks=4,
            cpus_per_task=8,
            memory="16G",
            queue="short",
            account="proj1",
        )
        lines = self.scheduler.directives("job", preset, "job.log")
        self.assertIn("#SBATCH --job-name=job", lines)
        self.assertIn("#SBATCH --time=02:00:00", lines)
        self.assertIn("#SBATCH --nodes=2", lines)
        self.assertIn("#SBATCH --ntasks=4", lines)
        self.assertIn("#SBATCH --cpus-per-task=8", lines)
        self.assertIn("#SBATCH --mem=16G", lines)
        self.assertIn("#SBATCH --partition=short", lines)
        self.assertIn("#SBATCH --account=proj1", lines)

    def test_single_cpu_omits_cpus_per_task(self):
        lines = self.scheduler.directives("job", SubmitPreset(cpus_per_task=1), "job.log")
        self.assertFalse(any("cpus-per-task" in line for line in lines))

    def test_empty_optional_fields_are_omitted(self):
        lines = self.scheduler.directives("job", SubmitPreset(walltime="", memory=""), "job.log")
        self.assertFalse(any("--mem" in line for line in lines))
        self.assertFalse(any("--time" in line for line in lines))

    def test_submit_uses_parsable(self):
        self.assertIn("--parsable", self.scheduler.submit_command("run.sh", "job.log"))

    def test_parse_parsable_output(self):
        self.assertEqual(self.scheduler.parse_submit_output("4823917\n", ""), "4823917")

    def test_parse_parsable_output_with_cluster_suffix(self):
        self.assertEqual(self.scheduler.parse_submit_output("4823917;mycluster\n", ""), "4823917")

    def test_parse_human_readable_output(self):
        self.assertEqual(self.scheduler.parse_submit_output("Submitted batch job 991\n", ""), "991")

    def test_parse_submit_failure_returns_empty(self):
        self.assertEqual(self.scheduler.parse_submit_output("", "sbatch: error"), "")

    def test_status_command_is_one_call_for_the_whole_user(self):
        cmd = self.scheduler.status_command("alice", ["1", "2", "3"])
        self.assertEqual(cmd.count("squeue"), 1)
        self.assertIn("-u alice", cmd)

    def test_parse_status(self):
        out = "101 PENDING\n102 RUNNING\n103 COMPLETING\n"
        self.assertEqual(
            self.scheduler.parse_status(out),
            {"101": STATE_PENDING, "102": STATE_RUNNING, "103": STATE_COMPLETING},
        )

    def test_parse_status_ignores_blank_and_short_lines(self):
        self.assertEqual(self.scheduler.parse_status("\n \n999\n"), {})

    def test_parse_status_maps_array_tasks_to_the_parent(self):
        states = self.scheduler.parse_status("55_3 RUNNING\n")
        self.assertEqual(states["55_3"], STATE_RUNNING)
        self.assertEqual(states["55"], STATE_RUNNING)

    def test_unknown_state_code(self):
        self.assertEqual(self.scheduler.parse_status("1 WEIRD\n"), {"1": STATE_UNKNOWN})

    def test_state_with_a_parenthesised_reason(self):
        self.assertEqual(
            self.scheduler.parse_status("1 PENDING(Resources)\n"), {"1": STATE_PENDING}
        )

    def test_cancel(self):
        self.assertEqual(self.scheduler.cancel_command("77"), "scancel 77")


class TestPbs(ScriptContractMixin, unittest.TestCase):
    scheduler_name = "pbs"

    def setUp(self):
        self.scheduler = get_scheduler("pbs")

    def test_directives(self):
        preset = SubmitPreset(walltime="10:00:00", nodes=2, cpus_per_task=4, queue="batch")
        lines = self.scheduler.directives("job", preset, "job.log")
        self.assertIn("#PBS -N job", lines)
        self.assertIn("#PBS -l walltime=10:00:00", lines)
        self.assertIn("#PBS -l nodes=2:ppn=4", lines)
        self.assertIn("#PBS -q batch", lines)
        self.assertIn("#PBS -j oe", lines)

    def test_serial_job_omits_the_nodes_line(self):
        lines = self.scheduler.directives("job", SubmitPreset(nodes=1, cpus_per_task=1), "job.log")
        self.assertFalse(any("ppn=" in line for line in lines))

    def test_parse_submit_output_with_a_host_suffix(self):
        self.assertEqual(
            self.scheduler.parse_submit_output("58231.headnode.cluster\n", ""),
            "58231.headnode.cluster",
        )

    def test_parse_submit_output_plain(self):
        self.assertEqual(self.scheduler.parse_submit_output("58231\n", ""), "58231")

    def test_parse_status(self):
        out = (
            "headnode:\n"
            "                                                            Req'd  Req'd   Elap\n"
            "Job ID    Username Queue    Jobname   SessID NDS TSK Memory Time  S Time\n"
            "--------- -------- -------- --------- ------ --- --- ------ ----- - -----\n"
            "58231.hea alice    batch    myjob      12345   1   4    --  10:00 R 00:12\n"
            "58232.hea alice    batch    other        --    1   1    --  10:00 Q   --\n"
        )
        states = self.scheduler.parse_status(out)
        self.assertEqual(states["58231.hea"], STATE_RUNNING)
        self.assertEqual(states["58232.hea"], STATE_PENDING)

    def test_parse_status_also_keys_the_bare_number(self):
        out = "58231.hea alice batch myjob 1 1 1 -- 10:00 R 00:12\n"
        self.assertEqual(self.scheduler.parse_status(out)["58231"], STATE_RUNNING)

    def test_parse_status_ignores_headers(self):
        self.assertEqual(self.scheduler.parse_status("Job ID Username Queue\n"), {})

    def test_cancel(self):
        self.assertEqual(self.scheduler.cancel_command("1.a"), "qdel 1.a")


class TestSge(ScriptContractMixin, unittest.TestCase):
    scheduler_name = "sge"

    def setUp(self):
        self.scheduler = get_scheduler("sge")

    def test_directives(self):
        preset = SubmitPreset(walltime="4:00:00", memory="8G", queue="all.q")
        lines = self.scheduler.directives("job", preset, "job.log")
        self.assertIn("#$ -N job", lines)
        self.assertIn("#$ -l h_rt=4:00:00", lines)
        self.assertIn("#$ -l h_vmem=8G", lines)
        self.assertIn("#$ -q all.q", lines)
        self.assertIn("#$ -cwd", lines)

    def test_parse_submit_output(self):
        out = 'Your job 4711 ("myjob") has been submitted\n'
        self.assertEqual(self.scheduler.parse_submit_output(out, ""), "4711")

    def test_parse_array_submit_output(self):
        out = 'Your job-array 4712.1-10:1 ("j") has been submitted\n'
        self.assertEqual(self.scheduler.parse_submit_output(out, ""), "4712")

    def test_parse_submit_failure(self):
        self.assertEqual(self.scheduler.parse_submit_output("", "denied"), "")

    def test_parse_status(self):
        out = (
            "job-ID  prior   name  user  state submit/start at     queue slots\n"
            "-----------------------------------------------------------------\n"
            "   4711 0.55500 myjob alice r     01/02/2026 10:00:00 all.q     1\n"
            "   4712 0.00000 other alice qw    01/02/2026 10:01:00           1\n"
        )
        states = self.scheduler.parse_status(out)
        self.assertEqual(states["4711"], STATE_RUNNING)
        self.assertEqual(states["4712"], STATE_PENDING)

    def test_error_state_stays_pending_because_the_job_is_still_queued(self):
        out = "   4713 0.0 j alice Eqw 01/02/2026 10:00:00 q 1\n"
        self.assertEqual(self.scheduler.parse_status(out)["4713"], STATE_PENDING)

    def test_deleting_state_maps_to_completing(self):
        out = "   4714 0.0 j alice dr 01/02/2026 10:00:00 q 1\n"
        self.assertEqual(self.scheduler.parse_status(out)["4714"], STATE_COMPLETING)

    def test_cancel(self):
        self.assertEqual(self.scheduler.cancel_command("9"), "qdel 9")


class TestShell(ScriptContractMixin, unittest.TestCase):
    scheduler_name = "shell"

    def setUp(self):
        self.scheduler = get_scheduler("shell")

    def test_submit_detaches_and_prints_the_pid(self):
        cmd = self.scheduler.submit_command("run.sh", "job.log")
        self.assertIn("nohup", cmd)
        self.assertIn("< /dev/null", cmd)
        self.assertTrue(cmd.rstrip().endswith("echo $!"))

    def test_parse_submit_output_takes_the_pid(self):
        self.assertEqual(self.scheduler.parse_submit_output("21841\n", ""), "21841")

    def test_parse_submit_output_ignores_leading_noise(self):
        out = "nohup: ignoring input\n21841\n"
        self.assertEqual(self.scheduler.parse_submit_output(out, ""), "21841")

    def test_status_command_lists_only_the_tracked_pids(self):
        cmd = self.scheduler.status_command("alice", ["11", "22"])
        self.assertIn("for p in 11 22", cmd)

    def test_status_command_uses_a_builtin_not_ps(self):
        # ps is absent or restricted on some hosts; kill -0 is a shell builtin.
        cmd = self.scheduler.status_command("alice", ["11"])
        self.assertIn("kill -0", cmd)
        self.assertNotIn("ps ", cmd)

    def test_status_command_never_fails_when_every_pid_is_gone(self):
        # A non-zero rc with no output would look like a broken host.
        self.assertTrue(self.scheduler.status_command("a", ["1"]).rstrip().endswith("true"))

    def test_status_command_with_no_pids_is_a_no_op(self):
        self.assertEqual(self.scheduler.status_command("alice", []), "true")

    def test_status_command_ignores_non_numeric_ids(self):
        self.assertIn("for p in 3", self.scheduler.status_command("a", ["x", "3"]))

    def test_parse_status_marks_live_pids_running(self):
        self.assertEqual(
            self.scheduler.parse_status(" 11\n 22\n"), {"11": STATE_RUNNING, "22": STATE_RUNNING}
        )

    def test_parse_status_of_an_empty_listing(self):
        self.assertEqual(self.scheduler.parse_status(""), {})

    def test_cancel_targets_the_process_group(self):
        self.assertIn("pgid", self.scheduler.cancel_command("42"))


class TestKillSignalTraps(unittest.TestCase):
    """Every generated script must turn a kill into a non-zero status."""

    def _script(self, name):
        return get_scheduler(name).build_script(
            "t", SubmitPreset(command_template="true"), "mol.inp", "job.log"
        )

    def test_all_three_signals_are_trapped(self):
        script = self._script("shell")
        for line in ("trap 'exit 143' TERM", "trap 'exit 130' INT", "trap 'exit 129' HUP"):
            self.assertIn(line, script)

    def test_every_scheduler_carries_them(self):
        for name in ("slurm", "pbs", "sge", "shell"):
            self.assertIn("trap 'exit 143' TERM", self._script(name), name)

    def test_they_come_after_the_exit_trap_that_writes_the_sentinel(self):
        script = self._script("slurm")
        self.assertLess(script.index("' EXIT"), script.index("trap 'exit 143' TERM"))


if __name__ == "__main__":
    unittest.main()
