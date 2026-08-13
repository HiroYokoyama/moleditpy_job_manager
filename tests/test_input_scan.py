"""Reading the resources an input file already states.

The user types the memory and core request once, into the input. Asking them
to type it again into the wizard means keeping two copies of one fact in step,
and the copy the queue schedules on is the one that gets forgotten.

The trap this file exists for: **ORCA states memory per core.** ``%maxcore
3000`` with eight cores is a 24 GB job, and reading it as 3 GB would put three
of them on a 32 GB machine and have the operating system kill two.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from job_manager.input_scan import Resources, format_memory, scan, scan_text

ORCA = """! B3LYP def2-SVP Opt
%pal nprocs 8 end
%maxcore 3000
* xyz 0 1
H 0.0 0.0 0.0
*
"""

GAUSSIAN = """%chk=job.chk
%mem=16GB
%nprocshared=8
#p b3lyp/6-31G(d) opt freq

title

0 1
H  0.0 0.0 0.0

"""


class TestOrca(unittest.TestCase):
    def test_maxcore_is_multiplied_by_the_core_count(self):
        found = scan_text(ORCA)
        self.assertEqual(found.memory_mb, 3000 * 8)
        self.assertEqual(found.cores, 8)
        self.assertEqual(found.program, "ORCA")

    def test_the_bang_form_of_pal_is_understood(self):
        found = scan_text("! B3LYP PAL4 Opt\n%maxcore 2000\n")
        self.assertEqual(found.cores, 4)
        self.assertEqual(found.memory_mb, 8000)

    def test_a_serial_job_is_its_maxcore(self):
        self.assertEqual(scan_text("! HF\n%maxcore 4000\n").memory_mb, 4000)

    def test_cores_alone_are_still_worth_having(self):
        found = scan_text("! HF\n%pal nprocs 16 end\n")
        self.assertEqual(found.cores, 16)
        self.assertEqual(found.memory_mb, 0)

    def test_case_and_spacing_do_not_matter(self):
        found = scan_text("! HF\n%PAL NPROCS 4 END\n%MaxCore 1000\n")
        self.assertEqual(found.memory_mb, 4000)


class TestGaussian(unittest.TestCase):
    def test_mem_and_nprocshared(self):
        found = scan_text(GAUSSIAN)
        self.assertEqual(found.memory_mb, 16 * 1024)
        self.assertEqual(found.cores, 8)
        self.assertEqual(found.program, "Gaussian")

    def test_mem_is_a_total_not_a_per_core_figure(self):
        # Unlike ORCA. Multiplying here would reserve eight times too much.
        self.assertEqual(scan_text("%mem=8GB\n%nprocshared=8\n#p hf\n").memory_mb, 8 * 1024)

    def test_megabytes(self):
        self.assertEqual(scan_text("%mem=8000MB\n#p hf\n").memory_mb, 8000)

    def test_a_bare_number_is_words_which_is_gaussians_own_default(self):
        # A million words is eight million bytes: 7 MB, not a gigabyte and
        # certainly not the 8 TB that reading it as megawords produced.
        self.assertEqual(scan_text("%mem=1000000\n#p hf\n").memory_mb, 7)

    def test_megawords_are_still_megawords_when_said(self):
        self.assertEqual(scan_text("%mem=1000MW\n#p hf\n").memory_mb, 8000)

    def test_nproc_without_shared(self):
        self.assertEqual(scan_text("%nproc=4\n%mem=1GB\n#p hf\n").cores, 4)


class TestTheOtherPrograms(unittest.TestCase):
    def test_psi4(self):
        found = scan_text("memory 8 GB\nmolecule {\nH\n}\nenergy('scf')\n")
        self.assertEqual((found.program, found.memory_mb), ("Psi4", 8192))

    def test_nwchem(self):
        found = scan_text("start h2o\nmemory total 4000 mb\ngeometry\nend\ntask scf\n")
        self.assertEqual((found.program, found.memory_mb), ("NWChem", 4000))

    def test_qchem(self):
        found = scan_text("$rem\n  MEM_TOTAL 8000\n  METHOD b3lyp\n$end\n")
        self.assertEqual((found.program, found.memory_mb), ("Q-Chem", 8000))

    def test_gamess_megawords_are_eight_bytes_each(self):
        found = scan_text(" $CONTRL SCFTYP=RHF $END\n $SYSTEM MWORDS=250 $END\n")
        self.assertEqual((found.program, found.memory_mb), ("GAMESS", 2000))


class TestItRefusesToGuess(unittest.TestCase):
    def test_a_plain_xyz_states_nothing(self):
        self.assertFalse(scan_text("3\nwater\nO 0 0 0\nH 0 0 1\nH 0 1 0\n").found)

    def test_an_empty_file_states_nothing(self):
        self.assertFalse(scan_text("").found)

    def test_a_bare_memory_line_is_not_enough_to_claim_a_format(self):
        # "memory 8 GB" alone could be anything; Psi4 and NWChem each insist on
        # a marker of their own before claiming a file.
        self.assertFalse(scan_text("memory 8 GB\n").found)

    def test_a_malformed_number_is_no_request_rather_than_a_wrong_one(self):
        self.assertFalse(scan_text("%mem=abc\n#p hf\n").found)


class TestReadingFromDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="scan_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, text, name="mol.inp", encoding="utf-8"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding=encoding) as handle:
            handle.write(text)
        return path

    def test_a_file_is_read(self):
        self.assertEqual(scan(self.write(ORCA)).memory_mb, 24000)

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(scan(os.path.join(self.tmp, "nope.inp")).found)

    def test_a_directory_is_not_an_error(self):
        self.assertFalse(scan(self.tmp).found)

    def test_undecodable_bytes_do_not_stop_the_scan(self):
        path = os.path.join(self.tmp, "odd.inp")
        with open(path, "wb") as handle:
            handle.write(b"%mem=4GB\n#p hf\n\xff\xfe\x00rubbish\n")
        self.assertEqual(scan(path).memory_mb, 4096)

    def test_only_the_front_of_a_huge_file_is_read(self):
        # Every directive is in the preamble; a trajectory should not be read
        # into memory to find out it says nothing.
        path = self.write("%mem=2GB\n#p hf\n" + ("x" * 5_000_000))
        self.assertEqual(scan(path).memory_mb, 2048)


class TestFormattingItBack(unittest.TestCase):
    def test_whole_gigabytes(self):
        self.assertEqual(format_memory(8192), "8G")

    def test_anything_else_stays_megabytes(self):
        self.assertEqual(format_memory(24000), "24000M")

    def test_nothing_is_nothing(self):
        self.assertEqual(format_memory(0), "")

    def test_it_round_trips_through_the_parser(self):
        from job_manager.schedulers import parse_memory_mb

        for value in (1024, 8192, 24000, 512):
            self.assertEqual(parse_memory_mb(format_memory(value)), value)


class TestTheResourcesRecord(unittest.TestCase):
    def test_empty_is_not_found(self):
        self.assertFalse(Resources().found)

    def test_cores_alone_count_as_found(self):
        self.assertTrue(Resources(cores=4).found)


if __name__ == "__main__":
    unittest.main()
