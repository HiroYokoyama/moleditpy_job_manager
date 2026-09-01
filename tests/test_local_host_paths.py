"""A host that *is* this machine, and the directories it owns.

``equal_path`` says "the host's filesystem is also reachable from here", and
everything built on it -- opening a result without downloading it, and picking
the host a dropped file already belongs to -- was written for a host reached
over a network. A local host is the same statement made trivially true: its
job directory is a path on this disk under exactly that name. Nothing treated
it that way, and the Hosts dialog does not offer it the field, so a local job's
own output was listed as "On Host (Not Downloaded)" and had to be copied to a
second place before it could be opened.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from job_manager import host_stats
from job_manager.models import (
    BACKEND_LOCAL,
    BACKEND_OPENSSH,
    BACKEND_WSL,
    SCHEDULER_SHELL,
    HostProfile,
)
from job_manager.store import JobStore

from .bash_support import BASH, bash_path


class TestALocalHostOwnsItsOwnRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="localroot_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.host = HostProfile(
            name="here", backend=BACKEND_LOCAL, scheduler=SCHEDULER_SHELL, remote_root=self.root
        )

    def test_a_file_under_the_root_belongs_to_it(self):
        self.assertTrue(self.host.owns_local_path(os.path.join(self.root, "run1", "mol.inp")))

    def test_the_root_itself_belongs_to_it(self):
        self.assertTrue(self.host.owns_local_path(self.root))

    def test_a_file_outside_it_does_not(self):
        outside = tempfile.mkdtemp(prefix="elsewhere_")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        self.assertFalse(self.host.owns_local_path(os.path.join(outside, "mol.inp")))

    def test_a_sibling_directory_does_not_count_as_inside(self):
        # Prefix matching without a separator boundary would call
        # ".../localroot_x_other" a child of ".../localroot_x".
        self.assertFalse(self.host.owns_local_path(self.root + "_other"))

    def test_local_root_is_the_remote_root(self):
        self.assertEqual(
            os.path.normcase(self.host.local_root()), os.path.normcase(os.path.abspath(self.root))
        )

    def test_a_remote_host_still_uses_its_equal_path(self):
        host = HostProfile(name="c", backend=BACKEND_OPENSSH, remote_root="/data/jobs")
        self.assertEqual(host.local_root(), "")
        host.equal_path = self.root
        self.assertTrue(host.owns_local_path(os.path.join(self.root, "mol.inp")))

    def test_a_remote_hosts_remote_root_is_not_a_local_path(self):
        # /data/jobs on a cluster says nothing about /data/jobs here.
        host = HostProfile(name="c", backend=BACKEND_OPENSSH, remote_root=self.root)
        self.assertFalse(host.owns_local_path(os.path.join(self.root, "mol.inp")))

    def test_wsl_is_excluded_although_it_is_local(self):
        # is_local is true for WSL, but its remote root names a path inside the
        # distribution's own filesystem, which this side cannot open by name.
        host = HostProfile(name="w", backend=BACKEND_WSL, remote_root="/home/u/jobs")
        self.assertTrue(host.is_local)
        self.assertEqual(host.local_root(), "")
        self.assertFalse(host.owns_local_path("/home/u/jobs/mol.inp"))


class TestTheJobDirectoryOfALocalHost(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="localjob_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.host = HostProfile(
            name="here", backend=BACKEND_LOCAL, scheduler=SCHEDULER_SHELL, remote_root=self.root
        )

    def test_it_resolves_to_itself(self):
        job_dir = os.path.join(self.root, "20260101_mol_ab")
        self.assertEqual(
            os.path.normcase(self.host.mirrored_job_dir(job_dir)),
            os.path.normcase(os.path.abspath(job_dir)),
        )

    def test_the_forward_slashes_remote_paths_builds_with_are_normalised(self):
        # remote_paths.join is posixpath.join, so a local Windows job directory
        # arrives as "C:\jobs/20260101_mol_ab".
        job_dir = self.root.replace("\\", "/") + "/20260101_mol_ab"
        resolved = self.host.mirrored_job_dir(job_dir)
        self.assertTrue(resolved)
        self.assertEqual(
            os.path.normcase(resolved),
            os.path.normcase(os.path.abspath(os.path.join(self.root, "20260101_mol_ab"))),
        )

    def test_no_directory_means_no_answer(self):
        self.assertEqual(self.host.mirrored_job_dir(""), "")

    def test_it_needs_no_equal_path(self):
        self.assertEqual(self.host.equal_path, "")
        self.assertTrue(self.host.mirrored_job_dir(os.path.join(self.root, "run1")))


class TestAMirroredHostsRootDirectory(unittest.TestCase):
    """The job that runs in the remote root itself, not below it."""

    def setUp(self):
        self.host = HostProfile(
            name="c", backend=BACKEND_OPENSSH, remote_root="/data/jobs", equal_path="/mnt/cluster"
        )

    def test_a_directory_below_the_root_maps_under_the_mirror(self):
        self.assertEqual(
            self.host.mirrored_job_dir("/data/jobs/run1"),
            os.path.join("/mnt/cluster", "run1"),
        )

    def test_the_root_itself_maps_to_the_mirror_root(self):
        # It used to answer "": the branch computed an empty relative path and
        # handed it to mirrored_path, which returns "" for a path with no
        # segments -- so the `else mirror_root` fallback was unreachable.
        self.assertEqual(self.host.mirrored_job_dir("/data/jobs"), "/mnt/cluster")

    def test_a_trailing_slash_does_not_change_that(self):
        self.assertEqual(self.host.mirrored_job_dir("/data/jobs/"), "/mnt/cluster")

    def test_a_sibling_of_the_root_still_maps_to_nothing(self):
        self.assertEqual(self.host.mirrored_job_dir("/data/jobs_other/run1"), "")

    def test_an_unrelated_directory_maps_to_nothing(self):
        self.assertEqual(self.host.mirrored_job_dir("/scratch/run1"), "")

    def test_a_posix_mirror_root_is_not_given_a_drive_letter(self):
        # abspath on Windows would turn /mnt/cluster into G:\mnt\cluster.
        self.assertTrue(self.host.mirrored_job_dir("/data/jobs").startswith("/mnt/cluster"))


class TestPickingTheHostAFileBelongsTo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ownerstore_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "jobs")
        os.makedirs(self.root)
        self.store = JobStore(self.tmp)

    def test_a_local_host_is_found_for_a_file_in_its_root(self):
        self.store.add_host(HostProfile(name="cluster", backend=BACKEND_OPENSSH))
        local = self.store.add_host(
            HostProfile(
                name="here",
                backend=BACKEND_LOCAL,
                scheduler=SCHEDULER_SHELL,
                remote_root=self.root,
            )
        )
        found = self.store.host_for_local_path(os.path.join(self.root, "run1", "mol.inp"))
        self.assertIsNotNone(found)
        self.assertEqual(found.id, local.id)

    def test_a_disabled_local_host_is_not_offered(self):
        self.store.add_host(
            HostProfile(
                name="here",
                backend=BACKEND_LOCAL,
                scheduler=SCHEDULER_SHELL,
                remote_root=self.root,
                enabled=False,
            )
        )
        self.assertIsNone(self.store.host_for_local_path(os.path.join(self.root, "mol.inp")))

    def test_the_more_specific_root_still_wins(self):
        inner = os.path.join(self.root, "inner")
        os.makedirs(inner)
        self.store.add_host(HostProfile(name="outer", backend=BACKEND_LOCAL, remote_root=self.root))
        deep = self.store.add_host(
            HostProfile(name="inner", backend=BACKEND_LOCAL, remote_root=inner)
        )
        found = self.store.host_for_local_path(os.path.join(inner, "mol.inp"))
        self.assertEqual(found.id, deep.id)


@unittest.skipIf(BASH is None, "no POSIX shell that can run a script")
class TestTheMemoryProbeReadsWhatItIsGiven(unittest.TestCase):
    """The awk in POSIX_COMMAND, run for real against each shape of meminfo.

    MemAvailable is the better number and is preferred wherever it exists, but
    it is not everywhere: the kernel gained it in 3.14, and Git Bash's emulated
    /proc/meminfo on Windows carries MemTotal and MemFree only. Reading it
    alone left mem_free unset, and the card drew an empty memory bar beside
    "15.6 GB total".
    """

    #: The awk program exactly as the shipped command spells it.
    AWK = re.search(r"awk '(/\^MemTotal:.*?)' /proc/meminfo", host_stats.POSIX_COMMAND, re.S)

    def parse_meminfo(self, text: str) -> host_stats.HostStats:
        self.assertIsNotNone(self.AWK, "the meminfo awk program moved; update this test")
        directory = tempfile.mkdtemp(prefix="meminfo_")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "meminfo")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        script = f"awk '{self.AWK.group(1)}' {bash_path(path)}"
        proc = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return host_stats.parse(proc.stdout)

    def test_a_modern_linux_uses_memavailable(self):
        stats = self.parse_meminfo(
            "MemTotal:       16369380 kB\nMemFree:         3292104 kB\n"
            "MemAvailable:    8000000 kB\n"
        )
        self.assertEqual(stats.mem_total_mb, 16369380 // 1024)
        self.assertEqual(stats.mem_free_mb, 8000000 // 1024)

    def test_memfree_carries_it_where_memavailable_is_absent(self):
        # Git Bash on Windows, and any kernel older than 3.14.
        stats = self.parse_meminfo(
            "MemTotal:       16369380 kB\nMemFree:         3292104 kB\nHighTotal: 0 kB\n"
        )
        self.assertEqual(stats.mem_total_mb, 16369380 // 1024)
        self.assertEqual(stats.mem_free_mb, 3292104 // 1024)
        self.assertGreater(stats.memory_fraction, 0.0)

    def test_the_bar_is_no_longer_empty(self):
        stats = self.parse_meminfo("MemTotal: 16369380 kB\nMemFree: 3292104 kB\n")
        self.assertGreater(stats.mem_used_mb, 0)
        self.assertIn("/", stats.summary)

    def test_a_total_on_its_own_is_still_only_a_total(self):
        # Nothing is invented: with no free figure at all the card says how big
        # the machine is and draws no usage.
        stats = self.parse_meminfo("MemTotal: 16369380 kB\n")
        self.assertEqual(stats.mem_free_mb, 0)
        self.assertEqual(stats.memory_fraction, 0.0)
        self.assertIn("total", stats.summary)


if __name__ == "__main__":
    unittest.main()
