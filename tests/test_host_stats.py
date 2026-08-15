"""Reading a host's load and memory out of one command's output.

The parser is deliberately forgiving: a login banner, a warning from a dotfile
or a source that simply is not there must cost the fields that *were* answered.
"""

import subprocess
import unittest

from job_manager import host_stats

from .bash_support import find_bash

BASH = find_bash()


class TestParsing(unittest.TestCase):
    def test_a_complete_answer(self):
        stats = host_stats.parse("cores=8\nload=1.60 1.20 0.90\nmem_total=64000\nmem_free=16000\n")
        self.assertEqual(stats.cores, 8)
        self.assertEqual(stats.load, (1.60, 1.20, 0.90))
        self.assertEqual(stats.mem_total_mb, 64000)
        self.assertEqual(stats.mem_used_mb, 48000)
        self.assertTrue(stats.ok)

    def test_load_against_the_core_count(self):
        # A load equal to the cores is a full machine, which is where the bar
        # should be full -- not at some arbitrary 100.
        stats = host_stats.parse("cores=8\nload=8.0 8.0 8.0\n")
        self.assertEqual(stats.load_fraction, 1.0)
        self.assertEqual(host_stats.parse("cores=8\nload=2.0 2 2").load_fraction, 0.25)

    def test_an_overloaded_machine_stays_at_full(self):
        stats = host_stats.parse("cores=4\nload=99.0 99.0 99.0\n")
        self.assertEqual(stats.load_fraction, 1.0)
        self.assertIn("99.00", stats.summary)

    def test_memory_the_host_did_not_report(self):
        # macOS and some containers have no MemAvailable. Reporting "0.0 GB
        # used" would be inventing a number.
        stats = host_stats.parse("cores=4\nmem_total=16000\n")
        self.assertEqual(stats.mem_used_mb, 0)
        self.assertEqual(stats.memory_fraction, 0.0)
        self.assertIn("total", stats.summary)

    def test_noise_around_the_answer_is_ignored(self):
        stats = host_stats.parse(
            "Welcome to the cluster!\n"
            "bash: /etc/profile.d/broken.sh: line 3: warning\n"
            "cores=2\n"
            "\n"
            "load=0.10 0.20 0.30\n"
        )
        self.assertEqual(stats.cores, 2)
        self.assertEqual(stats.load[0], 0.10)

    def test_nothing_at_all(self):
        stats = host_stats.parse("")
        self.assertEqual(stats.cores, 0)
        self.assertEqual(stats.load, ())
        self.assertEqual(stats.load_fraction, 0.0)
        self.assertEqual(stats.summary, "no answer")

    def test_rubbish_values_do_not_raise(self):
        stats = host_stats.parse("cores=lots\nload=high\nmem_total=plenty\n")
        self.assertEqual(stats.cores, 0)
        self.assertEqual(stats.load, ())

    def test_an_error_is_what_the_summary_says(self):
        stats = host_stats.HostStats(error="ssh: connect: timed out")
        self.assertFalse(stats.ok)
        self.assertEqual(stats.summary, "ssh: connect: timed out")

    def test_the_windows_command_is_powershell(self):
        self.assertIn("Get-CimInstance", host_stats.command_for(True))
        self.assertNotIn("Get-CimInstance", host_stats.command_for(False))


@unittest.skipUnless(BASH, "needs a bash")
class TestTheProbeRunsForReal(unittest.TestCase):
    """The command is shell, so text assertions about it prove very little."""

    def run_probe(self) -> str:
        result = subprocess.run(
            [BASH, "-c", host_stats.POSIX_COMMAND],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_it_answers_with_parseable_lines(self):
        stats = host_stats.parse(self.run_probe())
        # Cores is the one field every Unix answers, one way or another.
        self.assertGreaterEqual(stats.cores, 1)

    def test_it_says_nothing_on_stderr_about_missing_sources(self):
        # /proc, sysctl and uptime are each guarded; a host without one of them
        # must not fill the log with errors every two seconds.
        result = subprocess.run(
            [BASH, "-c", host_stats.POSIX_COMMAND],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.stderr.strip(), "")

    def test_the_uptime_fallback_parses_both_flavours(self):
        # The path taken where there is no /proc/loadavg. Fed fixed output
        # rather than this machine's: Git Bash's uptime prints no load at all,
        # so testing the local one would measure the machine, not the pipeline.
        pipeline = "sed -n 's/.*load averages*:[ ]*//p' | tr -d ','"
        samples = {
            "linux": " 15:12:32 up 19:51,  1 user,  load average: 0.35, 0.44, 0.51",
            "macos": "15:12  up 3 days,  2:11, 3 users, load averages: 1.20 1.10 1.05",
        }
        for flavour, line in samples.items():
            result = subprocess.run(
                [BASH, "-c", f"echo load=$(echo {line!r} | {pipeline})"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            stats = host_stats.parse(result.stdout)
            self.assertEqual(len(stats.load), 3, f"{flavour}: {result.stdout!r}")
            self.assertGreater(stats.load[0], 0, flavour)


if __name__ == "__main__":
    unittest.main()


class TestCoresAreNotThreads(unittest.TestCase):
    """`nproc` counts logical processors, which is not what a core is."""

    def test_both_are_reported(self):
        stats = host_stats.parse("cores=8\nthreads=16\nload=8.0 8.0 8.0\n")
        self.assertEqual(stats.cores, 8)
        self.assertEqual(stats.threads, 16)

    def test_the_bar_is_scaled_to_cores(self):
        # A load of 8 on eight cores is a full machine, whatever the thread
        # count says; scaling to 16 would call it half full.
        stats = host_stats.parse("cores=8\nthreads=16\nload=8.0 8.0 8.0\n")
        self.assertEqual(stats.load_fraction, 1.0)

    def test_the_summary_names_threads_separately(self):
        stats = host_stats.parse("cores=6\nthreads=12\nload=1.0 1.0 1.0\n")
        self.assertIn("6 cores", stats.summary)
        self.assertIn("12 threads", stats.summary)

    def test_a_machine_without_hyperthreading_says_cores_only(self):
        stats = host_stats.parse("cores=4\nthreads=4\nload=1.0 1.0 1.0\n")
        self.assertIn("4 cores", stats.summary)
        self.assertNotIn("threads", stats.summary)

    def test_the_probe_asks_for_physical_cores(self):
        # The same detection the helper queue uses, not a bare nproc.
        self.assertIn("lscpu", host_stats.POSIX_COMMAND)
        self.assertIn("hw.physicalcpu", host_stats.POSIX_COMMAND)

    @unittest.skipUnless(BASH, "needs a bash")
    def test_the_real_probe_separates_them(self):
        result = subprocess.run(
            [BASH, "-c", host_stats.POSIX_COMMAND], capture_output=True, text=True, timeout=60
        )
        stats = host_stats.parse(result.stdout)
        self.assertGreaterEqual(stats.cores, 1)
        self.assertGreaterEqual(stats.threads, stats.cores)


class TestInstantaneousCPUStatParsing(unittest.TestCase):
    """Real-time CPU parsing and summary formatting."""

    def test_posix_command_samples_proc_stat(self):
        self.assertIn("/proc/stat", host_stats.POSIX_COMMAND)
        self.assertIn("sleep", host_stats.POSIX_COMMAND)

    def test_summary_reports_cpu_label(self):
        stats = host_stats.parse("cores=4\nload=2.50 1.00 0.50\nmem_total=16000\nmem_free=8000\n")
        self.assertIn("CPU 2.50 1.00 0.50", stats.summary)

    def test_instant_cpu_fraction_computation(self):
        stats = host_stats.parse("cores=4\nload=3.60 1.20 0.80\n")
        self.assertEqual(stats.load[0], 3.60)
        self.assertAlmostEqual(stats.load_fraction, 0.90)
