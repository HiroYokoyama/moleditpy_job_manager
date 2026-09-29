"""Submit options, per-host intervals, and what each queue is actually sent.

Pure Python: no Qt. The scheduler half of this file exists because none of
SLURM, PBS or SGE is available to CI, so the exact text each one receives is
the only thing that can be held here -- and every case below is a spelling a
real queue refuses or misreads.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from job_manager import runner
from job_manager.models import HostProfile, Job, SubmitPreset
from job_manager.schedulers import get_scheduler
from job_manager.schedulers.base import memory_request, submit_arguments, user_argument
from job_manager.schedulers.pbs import queue_job_name
from job_manager.schedulers.sge import sge_job_name
from job_manager.store import MAX_POLL_INTERVAL, MIN_POLL_INTERVAL, JobStore

from .fakes import FakeTransport, make_host

PBS_UNITS = {"M": "mb", "G": "gb", "T": "tb"}
LETTER_UNITS = {"M": "M", "G": "G", "T": "T"}


class TestMemoryRequest(unittest.TestCase):
    def test_pbs_wants_lower_case_byte_words(self):
        self.assertEqual(memory_request("8G", PBS_UNITS, "M"), "8gb")
        self.assertEqual(memory_request("8GB", PBS_UNITS, "M"), "8gb")
        self.assertEqual(memory_request("512m", PBS_UNITS, "M"), "512mb")

    def test_a_bare_number_is_megabytes_not_bytes(self):
        # PBS and SGE read a bare number as bytes: 8192 asked for 8 kB.
        self.assertEqual(memory_request("8192", PBS_UNITS, "M"), "8192mb")
        self.assertEqual(memory_request("8192", LETTER_UNITS, "M"), "8192M")

    def test_one_letter_queues_lose_the_b(self):
        self.assertEqual(memory_request("16GB", LETTER_UNITS, "M"), "16G")
        self.assertEqual(memory_request("16gb", LETTER_UNITS, "M"), "16G")
        self.assertEqual(memory_request("2T", LETTER_UNITS, "M"), "2T")

    def test_a_fraction_is_carried_in_megabytes(self):
        self.assertEqual(memory_request("1.5G", LETTER_UNITS, "M"), "1536M")
        self.assertEqual(memory_request("1.5G", PBS_UNITS, "M"), "1536mb")

    def test_what_it_cannot_read_is_passed_through(self):
        for text in ("lots", "8B", "4 GiB", "8K"):
            with self.subTest(text=text):
                self.assertEqual(memory_request(text, LETTER_UNITS, "M"), text)

    def test_empty_stays_empty(self):
        self.assertEqual(memory_request("", LETTER_UNITS, "M"), "")


class TestSubmitArguments(unittest.TestCase):
    def test_split_like_a_shell(self):
        self.assertEqual(
            submit_arguments("-W group_list=gr1 -l select=1:ncpus=8"),
            ["-W", "group_list=gr1", "-l", "select=1:ncpus=8"],
        )

    def test_host_then_preset(self):
        self.assertEqual(submit_arguments("-A host", "-q preset"), ["-A", "host", "-q", "preset"])

    def test_quoted_words_stay_one_argument(self):
        self.assertEqual(submit_arguments('-N "two words"'), ["-N", "'two words'"])

    def test_a_second_command_is_only_an_argument(self):
        words = submit_arguments("-q x; rm -rf ~ $(id)")
        self.assertIn("'x;'", words)
        self.assertIn("'~'", words)
        self.assertIn("'$(id)'", words)
        self.assertNotIn(";", [w for w in words if not w.startswith("'")])

    def test_empty_gives_nothing(self):
        self.assertEqual(submit_arguments("", "   "), [])

    def test_an_unbalanced_quote_is_an_error(self):
        with self.assertRaises(ValueError):
            submit_arguments('-N "unterminated')


class TestUserArgument(unittest.TestCase):
    def test_no_user_leaves_it_to_the_remote_shell(self):
        self.assertEqual(user_argument(""), '"$USER"')
        self.assertEqual(user_argument("$USER"), '"$USER"')

    def test_a_plain_name_is_unchanged(self):
        self.assertEqual(user_argument("alice"), "alice")

    def test_anything_else_is_quoted(self):
        self.assertEqual(user_argument("a;b"), "'a;b'")

    def test_every_queue_listing_uses_it(self):
        for name, verb in (("slurm", "squeue"), ("pbs", "qstat"), ("sge", "qstat")):
            with self.subTest(name=name):
                command = get_scheduler(name).status_command("$USER", ["1"])
                self.assertIn(verb, command)
                self.assertIn('-u "$USER"', command)


class TestSubmitCommands(unittest.TestCase):
    ARGS = ["-W", "group_list=gr1"]

    def test_options_go_between_the_verb_and_the_script(self):
        self.assertEqual(
            get_scheduler("pbs").submit_command("run.sh", "job.log", self.ARGS),
            "qsub -W group_list=gr1 run.sh",
        )
        self.assertEqual(
            get_scheduler("sge").submit_command("run.sh", "job.log", self.ARGS),
            "qsub -W group_list=gr1 run.sh",
        )
        self.assertEqual(
            get_scheduler("slurm").submit_command("run.sh", "job.log", ["--qos=long"]),
            "sbatch --parsable --qos=long run.sh",
        )

    def test_no_options_is_the_command_it_always_was(self):
        self.assertEqual(get_scheduler("pbs").submit_command("run.sh", "job.log"), "qsub run.sh")
        self.assertEqual(
            get_scheduler("slurm").submit_command("run.sh", "job.log"),
            "sbatch --parsable run.sh",
        )

    def test_the_built_in_modes_ignore_them(self):
        for name in ("shell", "windows"):
            with self.subTest(name=name):
                scheduler = get_scheduler(name)
                self.assertEqual(
                    scheduler.submit_command("run.sh", "job.log", self.ARGS),
                    scheduler.submit_command("run.sh", "job.log"),
                )


class TestPbsDirectives(unittest.TestCase):
    def setUp(self):
        self.pbs = get_scheduler("pbs")

    def lines(self, **preset):
        return self.pbs.directives("job", SubmitPreset(**preset), "job.log")

    def test_runs_under_bash_whatever_the_login_shell(self):
        self.assertIn("#PBS -S /bin/bash", self.lines())

    def test_memory_is_spelled_for_pbs(self):
        self.assertIn("#PBS -l mem=16gb", self.lines(memory="16G"))
        self.assertIn("#PBS -l mem=4096mb", self.lines(memory="4096"))

    def test_mpi_ranks_count_towards_ppn(self):
        self.assertIn("#PBS -l nodes=1:ppn=4", self.lines(ntasks=4, cpus_per_task=1))

    def test_ranks_times_threads_per_node(self):
        self.assertIn("#PBS -l nodes=2:ppn=8", self.lines(nodes=2, ntasks=4, cpus_per_task=4))

    def test_uneven_ranks_round_up(self):
        self.assertIn("#PBS -l nodes=2:ppn=3", self.lines(nodes=2, ntasks=5, cpus_per_task=1))

    def test_a_serial_job_asks_for_no_nodes_line(self):
        self.assertFalse(any("nodes=" in line for line in self.lines()))

    def test_a_name_starting_with_a_digit_is_prefixed(self):
        self.assertEqual(queue_job_name("2-butanol"), "j2-butanol")
        self.assertIn("#PBS -N j2-butanol", self.pbs.directives("2-butanol", SubmitPreset(), "l"))

    def test_a_long_name_is_cut_for_old_pbs(self):
        self.assertEqual(len(queue_job_name("a" * 40)), 15)

    def test_the_command_still_gets_the_whole_name(self):
        preset = SubmitPreset(command_template="prog {name}")
        script = self.pbs.build_script("2-butanol_conformer_search", preset, "", "job.log")
        self.assertIn("prog 2-butanol_conformer_search", script)


class TestPbsStatus(unittest.TestCase):
    HEADER = (
        "\nserver:\n"
        "                                                            Req'd  Req'd   Elap\n"
        "Job ID          Username Queue    Jobname    SessID NDS TSK Memory Time  S Time\n"
        "--------------- -------- -------- ---------- ------ --- --- ------ ----- - -----\n"
    )

    def parse(self, rows):
        return get_scheduler("pbs").parse_status(self.HEADER + rows)

    def test_a_completed_torque_row_counts_as_gone(self):
        # Kept as COMPLETING, a finished job waited out keep_completed.
        states = self.parse(
            "101.server      alice    batch    job        1234   1   4    --  01:00 C 00:10\n"
        )
        self.assertNotIn("101.server", states)
        self.assertNotIn("101", states)

    def test_finished_and_expired_pbs_pro_rows_count_as_gone(self):
        states = self.parse(
            "102.server      alice    batch    job        1234   1   4    --  01:00 F 00:10\n"
            "103.server      alice    batch    job        1234   1   4    --  01:00 X 00:10\n"
        )
        self.assertEqual(states, {})

    def test_running_and_exiting_rows_are_kept(self):
        states = self.parse(
            "104.server      alice    batch    job        1234   1   4    --  01:00 R 00:10\n"
            "105.server      alice    batch    job        1234   1   4    --  01:00 E 00:10\n"
        )
        self.assertEqual(states["104"], "RUNNING")
        self.assertEqual(states["105"], "COMPLETING")

    def test_a_completed_job_is_resolved_from_its_sentinel_straight_away(self):
        host = make_host(scheduler="pbs")
        job = Job(id="j1", host_id=host.id, remote_job_id="101.server", remote_dir="/jobs/j1")
        job.state = "RUNNING"
        transport = FakeTransport(host).when(
            "qstat",
            stdout=self.HEADER
            + "101.server      tester   batch    job        1234   1   4    --  01:00 C 00:10\n",
        )
        transport.when("@@MOLEDITPY@@", stdout="@@MOLEDITPY@@\n0\n")
        self.assertEqual(runner.poll_host(transport, host, [job]), {"j1": "DONE"})


class TestSubmitOutputWithABanner(unittest.TestCase):
    """The job's login files can print before qsub/sbatch does."""

    BANNER = "2026-10-01 maintenance 09:00-18:00\nWelcome to the cluster\n"

    def test_pbs_takes_the_line_qsub_printed(self):
        pbs = get_scheduler("pbs")
        self.assertEqual(pbs.parse_submit_output(self.BANNER + "4711.pbs01\n", ""), "4711.pbs01")

    def test_pbs_array_ids_are_read(self):
        pbs = get_scheduler("pbs")
        self.assertEqual(pbs.parse_submit_output("4711[].pbs01\n", ""), "4711[].pbs01")

    def test_pbs_banner_alone_is_no_job(self):
        self.assertEqual(get_scheduler("pbs").parse_submit_output(self.BANNER, ""), "")

    def test_slurm_takes_the_last_line(self):
        slurm = get_scheduler("slurm")
        self.assertEqual(slurm.parse_submit_output("12\n" + "4711;cluster\n", ""), "4711")


class TestOtherQueues(unittest.TestCase):
    def test_sge_memory_and_name(self):
        lines = get_scheduler("sge").directives("9mol", SubmitPreset(memory="8GB"), "job.log")
        self.assertIn("#$ -l h_vmem=8G", lines)
        self.assertIn("#$ -N j9mol", lines)
        self.assertEqual(sge_job_name("mol"), "mol")

    def test_slurm_memory(self):
        lines = get_scheduler("slurm").directives("job", SubmitPreset(memory="16GB"), "job.log")
        self.assertIn("#SBATCH --mem=16G", lines)


class TestNewFieldsRoundTrip(unittest.TestCase):
    def test_host_fields_survive_a_save(self):
        host = HostProfile(
            submit_options="-W group_list=g",
            monitor_usage=False,
            monitor_interval=60,
            poll_interval=600,
        )
        again = HostProfile.from_dict(host.to_dict())
        self.assertEqual(again.submit_options, "-W group_list=g")
        self.assertFalse(again.monitor_usage)
        self.assertEqual(again.monitor_interval, 60)
        self.assertEqual(again.poll_interval, 600)

    def test_an_older_profile_gets_the_old_behaviour(self):
        host = HostProfile.from_dict({"name": "old"})
        self.assertEqual(host.submit_options, "")
        self.assertTrue(host.monitor_usage)
        self.assertEqual(host.monitor_interval, 0)
        self.assertEqual(host.poll_interval, 0)

    def test_a_malformed_value_is_dropped(self):
        host = HostProfile.from_dict({"poll_interval": "fast", "monitor_usage": "no"})
        self.assertEqual(host.poll_interval, 0)
        self.assertTrue(host.monitor_usage)

    def test_preset_submit_options(self):
        preset = SubmitPreset.from_dict(SubmitPreset(submit_options="-q x").to_dict())
        self.assertEqual(preset.submit_options, "-q x")
        self.assertEqual(SubmitPreset.from_dict({}).submit_options, "")


class TestPollIntervalFor(unittest.TestCase):
    def setUp(self):
        self.store = JobStore(tempfile.mkdtemp(prefix="interval_"))
        self.store.set_pref("poll_interval", 120)

    def add(self, **overrides):
        host = make_host(**overrides)
        self.store.add_host(host)
        return host

    def test_no_override_follows_the_global(self):
        self.assertEqual(self.store.poll_interval_for(self.add().id), 120)

    def test_an_override_wins(self):
        self.assertEqual(self.store.poll_interval_for(self.add(poll_interval=900).id), 900)

    def test_an_override_is_clamped(self):
        self.assertEqual(
            self.store.poll_interval_for(self.add(id="a", poll_interval=1).id), MIN_POLL_INTERVAL
        )
        self.assertEqual(
            self.store.poll_interval_for(self.add(id="b", poll_interval=10**6).id),
            MAX_POLL_INTERVAL,
        )

    def test_an_unknown_host_follows_the_global(self):
        self.assertEqual(self.store.poll_interval_for("nobody"), 120)


class TestSubmitJobCarriesTheOptions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="options_")
        self.input_path = os.path.join(self.tmp, "mol.inp")
        with open(self.input_path, "w", encoding="utf-8") as handle:
            handle.write("! B3LYP\n")

    def submit(self, host, preset):
        transport = FakeTransport(host).when("qsub", stdout="4711.pbs01\n")
        job = runner.submit_job(transport, host, preset, Job(name="mol"), [self.input_path])
        return job, transport

    def test_host_then_preset_with_placeholders(self):
        host = make_host(scheduler="pbs", submit_options="-W group_list=gr1")
        preset = SubmitPreset(
            command_template="prog {input}",
            cpus_per_task=8,
            submit_options="-l select=1:ncpus={cpus}",
        )
        job, transport = self.submit(host, preset)
        submit = [c for c in transport.commands if "qsub" in c][0]
        self.assertIn("qsub -W group_list=gr1 -l select=1:ncpus=8 ", submit)
        self.assertEqual(job.remote_job_id, "4711.pbs01")

    def test_an_unreadable_option_stops_before_the_host_is_touched(self):
        host = make_host(scheduler="pbs")
        preset = SubmitPreset(command_template="prog {input}", submit_options='-N "oops')
        transport = FakeTransport(host)
        with self.assertRaises(ValueError) as caught:
            runner.submit_job(transport, host, preset, Job(name="mol"), [self.input_path])
        self.assertIn("Submit options", str(caught.exception))
        self.assertEqual(transport.commands, [])
        self.assertEqual(transport.uploads, [])


class TestTheApiAcceptsThem(unittest.TestCase):
    def test_submit_options_is_a_preset_field(self):
        from job_manager.api_core import PRESET_FIELDS

        self.assertIs(PRESET_FIELDS["submit_options"], str)


if __name__ == "__main__":
    unittest.main()
