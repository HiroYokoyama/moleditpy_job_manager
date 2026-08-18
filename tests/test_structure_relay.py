"""Relaying a file from a finished job into a new job's input.

Pure Python, no transport, no Qt: everything here is text substitution and a
filesystem write, all done before a file is ever uploaded. The actual remote
copy this sets up for is exercised in test_runner.py / test_submission_paths.py,
where the transport is real.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from job_manager.models import STATE_DONE, STATE_RUNNING, Job
from job_manager.structure_relay import (
    StructureRelayError,
    candidate_jobs,
    find_tags,
    materialize,
    relay_plan,
    resolve_filename,
    substitute_paths,
)


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relay_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def job(self, **overrides) -> Job:
        fields = dict(
            id="src", name="opt", state=STATE_DONE, host_id="h1", remote_dir="/home/t/src"
        )
        fields.update(overrides)
        return Job(**fields)


class TestFindingTags(unittest.TestCase):
    def test_a_bare_tag_is_found(self):
        self.assertEqual(len(find_tags("before [prevfile] after")), 1)

    def test_an_extension_tag_is_found(self):
        matches = find_tags("%oldchk=[prevfile:.chk]")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].group("ext"), ".chk")

    def test_any_extension_is_accepted(self):
        for ext in (".xyz", ".chk", ".res", ".fchk", ".gbw"):
            matches = find_tags(f"[prevfile:{ext}]")
            self.assertEqual(matches[0].group("ext"), ext)

    def test_no_tag_finds_nothing(self):
        self.assertEqual(find_tags("nothing here"), [])

    def test_two_different_tags_are_both_found(self):
        matches = find_tags("[prevfile:.chk] and [prevfile:.xyz]")
        self.assertEqual([m.group("ext") for m in matches], [".chk", ".xyz"])


class TestResolvingTheFilename(RelayCase):
    def test_named_after_the_uploaded_input(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        self.assertEqual(resolve_filename(job, ".chk"), "mol.chk")

    def test_falls_back_to_the_job_name_with_no_input_file(self):
        job = self.job(name="staged_job", input_files=[])
        self.assertEqual(resolve_filename(job, ".xyz"), "staged_job.xyz")

    def test_any_extension_works(self):
        job = self.job(input_files=[os.path.join(self.tmp, "run.inp")])
        for ext in (".res", ".fchk", ".gbw"):
            self.assertEqual(resolve_filename(job, ext), f"run{ext}")


class TestNestedPaths(RelayCase):
    """A relayed file inside a folder of its own: <stem>.res/<stem>.xyz."""

    def test_a_slash_form_resolves_to_a_nested_path(self):
        job = self.job(input_files=[os.path.join(self.tmp, "opt.inp")])
        self.assertEqual(resolve_filename(job, ".res/.xyz"), "opt.res/opt.xyz")

    def test_the_folder_and_file_extensions_can_differ(self):
        job = self.job(input_files=[os.path.join(self.tmp, "opt.inp")])
        self.assertEqual(resolve_filename(job, ".res/.gbw"), "opt.res/opt.gbw")

    def test_the_tag_is_found_in_text(self):
        matches = find_tags("[prevfile:.res/.xyz]")
        self.assertEqual(matches[0].group("ext"), ".res/.xyz")

    def test_substitution_writes_the_nested_path(self):
        job = self.job(input_files=[os.path.join(self.tmp, "opt.inp")])
        result = substitute_paths("xyzfile [prevfile:.res/.xyz]", job)
        self.assertEqual(result, "xyzfile opt.res/opt.xyz")

    def test_the_relay_plan_includes_the_nested_path(self):
        job = self.job(input_files=[os.path.join(self.tmp, "opt.inp")])
        self.assertEqual(relay_plan("[prevfile:.res/.xyz]", job), ["opt.res/opt.xyz"])

    def test_a_flat_and_a_nested_tag_can_coexist(self):
        job = self.job(input_files=[os.path.join(self.tmp, "opt.inp")])
        plan = relay_plan("[prevfile:.chk] [prevfile:.res/.xyz]", job)
        self.assertEqual(sorted(plan), ["opt.chk", "opt.res/opt.xyz"])


class TestSubstitution(RelayCase):
    def test_the_tag_is_replaced_with_the_resolved_filename(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        result = substitute_paths("%oldchk=[prevfile:.chk]", job)
        self.assertEqual(result, "%oldchk=mol.chk")

    def test_two_tags_are_both_replaced(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        result = substitute_paths("[prevfile:.chk] [prevfile:.xyz]", job)
        self.assertEqual(result, "mol.chk mol.xyz")

    def test_no_tag_at_all_is_refused(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        with self.assertRaises(StructureRelayError):
            substitute_paths("nothing to fill in here", job)

    def test_a_bare_tag_with_no_extension_is_refused(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        with self.assertRaises(StructureRelayError):
            substitute_paths("[prevfile]", job)


class TestTheRelayPlan(RelayCase):
    def test_one_file_per_distinct_extension(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        plan = relay_plan("[prevfile:.chk] ... [prevfile:.xyz]", job)
        self.assertEqual(sorted(plan), ["mol.chk", "mol.xyz"])

    def test_the_same_extension_twice_is_one_entry(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        plan = relay_plan("[prevfile:.chk] and again [prevfile:.chk]", job)
        self.assertEqual(plan, ["mol.chk"])

    def test_a_bare_tag_contributes_nothing_to_the_plan(self):
        # It is refused by substitute_paths(); the plan simply skips what it
        # cannot resolve rather than raising, since materialize() is what
        # actually enforces the input is usable.
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        self.assertEqual(relay_plan("[prevfile]", job), [])

    def test_no_tags_means_an_empty_plan(self):
        job = self.job(input_files=[os.path.join(self.tmp, "mol.inp")])
        self.assertEqual(relay_plan("plain text", job), [])


class TestListingCandidateJobs(RelayCase):
    def test_a_running_job_is_offered_too(self):
        # Relaying from a job that has not finished yet is intentional -- the
        # dialog chains the new submission behind it. See structure_relay.py.
        done = self.job(id="a", state=STATE_DONE, finished_at=10.0)
        running = self.job(id="b", state=STATE_RUNNING)
        offered = candidate_jobs([done, running])
        self.assertEqual({job.id for job in offered}, {"a", "b"})

    def test_a_failed_job_is_not_offered(self):
        from job_manager.models import STATE_FAILED

        done = self.job(id="a", state=STATE_DONE, finished_at=10.0)
        failed = self.job(id="b", state=STATE_FAILED)
        offered = candidate_jobs([done, failed])
        self.assertEqual([job.id for job in offered], ["a"])

    def test_a_job_with_no_remote_dir_is_not_offered(self):
        done = self.job(id="a", state=STATE_DONE, finished_at=10.0, remote_dir="")
        offered = candidate_jobs([done])
        self.assertEqual(offered, [])

    def test_restricted_to_one_host_when_asked(self):
        here = self.job(id="a", host_id="h1", state=STATE_DONE, finished_at=1.0)
        there = self.job(id="b", host_id="h2", state=STATE_DONE, finished_at=2.0)
        offered = candidate_jobs([here, there], host_id="h1")
        self.assertEqual([job.id for job in offered], ["a"])

    def test_every_host_offered_with_no_restriction(self):
        here = self.job(id="a", host_id="h1", state=STATE_DONE, finished_at=1.0)
        there = self.job(id="b", host_id="h2", state=STATE_DONE, finished_at=2.0)
        offered = candidate_jobs([here, there])
        self.assertEqual({job.id for job in offered}, {"a", "b"})

    def test_newest_finished_first(self):
        older = self.job(id="a", state=STATE_DONE, finished_at=10.0)
        newer = self.job(id="b", state=STATE_DONE, finished_at=99.0)
        offered = candidate_jobs([older, newer])
        self.assertEqual([job.id for job in offered], ["b", "a"])


class TestMaterializing(RelayCase):
    def source_job(self) -> Job:
        return self.job(input_files=[os.path.join(self.tmp, "opt.inp")])

    def test_a_tagged_file_is_filled_in(self):
        template = self.write("run.inp", "%oldchk=[prevfile:.chk]\n# something\n")
        result = materialize(template, self.source_job())
        text = open(result, encoding="utf-8").read()
        self.assertEqual(text, "%oldchk=opt.chk\n# something\n")

    def test_the_result_keeps_the_original_basename(self):
        template = self.write("run.inp", "[prevfile:.xyz]\n")
        result = materialize(template, self.source_job())
        self.assertEqual(os.path.basename(result), "run.inp")

    def test_the_original_file_is_never_touched(self):
        template = self.write("run.inp", "[prevfile:.xyz]\n")
        before = open(template, encoding="utf-8").read()
        materialize(template, self.source_job())
        after = open(template, encoding="utf-8").read()
        self.assertEqual(before, after)

    def test_the_result_lands_somewhere_else(self):
        template = self.write("run.inp", "[prevfile:.xyz]\n")
        result = materialize(template, self.source_job())
        self.assertNotEqual(os.path.dirname(result), os.path.dirname(template))

    def test_a_file_with_no_tag_is_refused(self):
        template = self.write("run.inp", "! opt\n* xyz 0 1\nO 0 0 0\n*\n")
        with self.assertRaises(StructureRelayError):
            materialize(template, self.source_job())

    def test_a_missing_input_file_is_reported_plainly(self):
        with self.assertRaises(StructureRelayError):
            materialize(os.path.join(self.tmp, "nope.inp"), self.source_job())


if __name__ == "__main__":
    unittest.main()
