"""The docs make checkable claims; this checks them.

The README described the completion sentinel as a trailing ``echo`` for two
commits after it became an EXIT trap, which is exactly the kind of drift nobody
notices until it misleads someone. Anything in the docs that restates a number,
a command or a snippet from the code is asserted here instead of trusted.
"""

import os
import pathlib
import unittest
from unittest.mock import patch

import pytest

from job_manager import runner, store
from job_manager.command_templates import TEMPLATES
from job_manager.models import SubmitPreset
from job_manager.schedulers import get_scheduler

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"


def read(path):
    return path.read_text(encoding="utf-8")


def _poller():
    """The poller module, or a skip: it imports PyQt6, which the pytest-only
    CI job does not install."""
    pytest.importorskip("PyQt6.QtCore", reason="PyQt6 is not installed")
    from job_manager import poller

    return poller


class TestTheDocsExist(unittest.TestCase):
    def test_the_three_documents_are_present(self):
        for name in ("WORKFLOW.md", "ARCHITECTURE.md", "SECURITY_MODEL.md"):
            self.assertTrue((DOCS / name).is_file(), name)

    def test_the_readme_links_all_of_them(self):
        text = read(README)
        for name in ("WORKFLOW.md", "ARCHITECTURE.md", "SECURITY_MODEL.md"):
            self.assertIn(f"docs/{name}", text, name)

    def test_every_doc_link_resolves(self):
        for document in [README] + sorted(DOCS.glob("*.md")):
            text = read(document)
            for target in _local_links(text):
                resolved = (document.parent / target).resolve()
                self.assertTrue(resolved.exists(), f"{document.name} -> {target}")


class TestDocumentedNumbers(unittest.TestCase):
    def test_poll_interval_bounds(self):
        text = read(DOCS / "WORKFLOW.md")
        self.assertIn(f"every {store.DEFAULT_POLL_INTERVAL} s", text)
        self.assertIn(f"down to {store.MIN_POLL_INTERVAL} s", text)
        self.assertIn(f"under {store.RECOMMENDED_MIN_POLL_INTERVAL} s", text)

    def test_the_backoff_ceiling(self):
        poller = _poller()
        minutes = poller.MAX_BACKOFF // 60
        self.assertIn(f"{minutes} minutes", read(DOCS / "WORKFLOW.md"))
        self.assertIn(f"{minutes} minutes", read(DOCS / "ARCHITECTURE.md"))

    def test_the_manual_refresh_cooldown(self):
        seconds = int(_poller().MANUAL_REFRESH_COOLDOWN)
        self.assertIn(f"once every {seconds} s", read(DOCS / "WORKFLOW.md"))

    def test_the_tail_length(self):
        default = runner.tail_log.__defaults__[0]
        self.assertIn(f"last {default} lines", read(DOCS / "WORKFLOW.md"))

    def test_the_thread_pool_sizes(self):
        text = read(DOCS / "ARCHITECTURE.md")
        self.assertIn("`QThreadPool` (3)", text)
        self.assertIn("`QThreadPool` (2)", text)


class TestDocumentedScript(unittest.TestCase):
    """Snippets shown as "this is what runs" must be what actually runs."""

    def setUp(self):
        self.script = get_scheduler("slurm").build_script(
            "j", SubmitPreset(command_template="x"), "mol.inp", "job.log"
        )

    def test_the_trap_lines_are_quoted_verbatim(self):
        for document in (README, DOCS / "ARCHITECTURE.md"):
            text = read(document)
            for line in (
                "trap 'exit 143' TERM",
                "trap 'exit 130' INT",
                "trap 'exit 129' HUP",
            ):
                self.assertIn(line, self.script)
                self.assertIn(line, text, f"{document.name}: {line}")

    def test_the_exit_trap_is_quoted_verbatim(self):
        exit_trap = next(line for line in self.script.splitlines() if line.endswith("' EXIT"))
        for document in (README, DOCS / "ARCHITECTURE.md"):
            self.assertIn(exit_trap, read(document), document.name)

    def test_the_sentinel_file_name(self):
        from job_manager.models import SENTINEL_NAME

        for document in (README, DOCS / "ARCHITECTURE.md"):
            self.assertIn(SENTINEL_NAME, read(document), document.name)


class TestDocumentedTemplates(unittest.TestCase):
    def test_every_built_in_command_appears_in_the_workflow_doc(self):
        text = read(DOCS / "WORKFLOW.md")
        for template in TEMPLATES:
            if template.command:
                self.assertIn(template.command, text, template.label)

    def test_every_placeholder_is_documented(self):
        from job_manager.schedulers.base import placeholder_values

        text = read(DOCS / "WORKFLOW.md")
        for tag in placeholder_values("mol.inp", SubmitPreset()):
            self.assertIn(tag, text, tag)

    def test_both_spellings_are_documented(self):
        text = read(DOCS / "WORKFLOW.md")
        self.assertIn("{input}", text)
        self.assertIn("[input]", text)


class TestDocumentedChainingAndScheduling(unittest.TestCase):
    """The per-scheduler tables are the sort of thing that silently rots."""

    def setUp(self):
        self.workflow = read(DOCS / "WORKFLOW.md")

    def test_every_dependency_mechanism_is_documented(self):
        for name in ("slurm", "pbs", "sge"):
            directive = get_scheduler(name).dependency_directives("12345")[0]
            # The doc writes <id> where a real script has the number.
            self.assertIn(directive.replace("12345", "<id>"), self.workflow, name)

    def test_the_no_queue_mechanism_is_described(self):
        self.assertIn("waits for the previous job's process", self.workflow)
        self.assertEqual(get_scheduler("shell").dependency_directives("12345"), [])

    def test_every_start_time_format_is_documented(self):
        import re

        for name, sample in (
            ("slurm", "#SBATCH --begin="),
            ("pbs", "#PBS -a "),
            ("sge", "#$ -a "),
        ):
            emitted = get_scheduler(name).start_time_directives(1786000000)[0]
            self.assertTrue(emitted.startswith(sample), emitted)
            self.assertIn(sample, self.workflow, name)
            # and the doc's example must be in the same shape the code emits
            shape = re.sub(r"\d", "0", emitted)
            documented = [
                re.sub(r"\d", "0", line) for line in self.workflow.splitlines() if sample in line
            ]
            self.assertIn(shape, " ".join(documented), name)

    def test_the_queued_state_is_documented(self):
        from job_manager.models import STATE_QUEUED

        self.assertIn(f"`{STATE_QUEUED}`", self.workflow)

    def test_the_blocked_state_is_documented(self):
        from job_manager.models import STATE_BLOCKED

        self.assertIn(f"`{STATE_BLOCKED}`", self.workflow)

    def test_the_afterany_form_is_documented(self):
        # Both spellings have to be named, since the whole point of the section
        # is that the user is choosing between them.
        for name in ("slurm", "pbs"):
            emitted = get_scheduler(name).dependency_directives("12345", any_outcome=True)[0]
            self.assertIn("afterany", emitted, name)
        self.assertIn("afterany", self.workflow)
        self.assertIn("afterok", self.workflow)

    def test_the_doc_names_the_right_schedulers_as_stranding_a_chain(self):
        # The row telling the user which queues strand a chain is the whole
        # point of the section; a wrong name there is worse than no row at all.
        stranding = sorted(
            name
            for name in ("slurm", "pbs", "sge", "shell")
            if not get_scheduler(name).chain_releases_on_failure
        )
        self.assertEqual(stranding, ["pbs", "slurm"])
        rows = [line for line in self.workflow.splitlines() if "never start" in line]
        self.assertTrue(rows, "the workflow doc no longer says a chain can be stranded")
        row = rows[0]
        self.assertIn("SLURM", row)
        self.assertIn("PBS", row)
        self.assertNotIn("SGE", row)


class TestDocumentedPaths(unittest.TestCase):
    def test_the_data_directory(self):
        expected = "~/.moleditpy/job_manager"
        for document in (README, DOCS / "SECURITY_MODEL.md", DOCS / "ARCHITECTURE.md"):
            self.assertIn(expected, read(document), document.name)
        # conftest points the override at a temp dir; ask for the real default.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(store.DATA_DIR_ENV, None)
            actual = store.default_data_dir()
        self.assertTrue(actual.replace("\\", "/").endswith(expected[2:]), actual)

    def test_the_settings_and_jobs_file_names(self):
        text = read(DOCS / "SECURITY_MODEL.md")
        self.assertIn(store.SETTINGS_FILENAME, text)
        self.assertIn(store.JOBS_FILENAME, text)


class TestSecurityClaims(unittest.TestCase):
    """The security doc states properties; each is checked in code elsewhere,
    so here we only confirm the claim and the mechanism have not diverged."""

    def test_the_host_profile_really_has_no_password_field(self):
        from dataclasses import fields

        from job_manager.models import HostProfile

        names = {f.name for f in fields(HostProfile)}
        self.assertNotIn("password", names)
        self.assertIn("ask_password", names)
        self.assertIn("no password field", read(DOCS / "SECURITY_MODEL.md"))

    def test_batch_mode_is_really_always_set(self):
        from job_manager.transport.openssh import OpenSSHTransport

        from .fakes import make_host

        options = OpenSSHTransport(make_host())._common_options()
        self.assertIn("BatchMode=yes", options)
        self.assertIn("BatchMode=yes", read(DOCS / "SECURITY_MODEL.md"))

    def test_the_sanitising_examples_are_real(self):
        from job_manager.models import sanitize_name

        text = read(DOCS / "SECURITY_MODEL.md")
        self.assertEqual(sanitize_name("../../etc/passwd"), "etc_passwd")
        self.assertEqual(sanitize_name("a;rm -rf /"), "a_rm_-rf")
        self.assertIn("etc_passwd", text)
        self.assertIn("a_rm_-rf", text)

    def test_the_named_test_module_exists(self):
        self.assertIn("tests/test_credentials.py", read(DOCS / "SECURITY_MODEL.md"))
        self.assertTrue((ROOT / "tests" / "test_credentials.py").is_file())


def _local_links(text):
    """Relative markdown link targets, ignoring URLs and anchors."""
    import re

    for match in re.finditer(r"\]\(([^)]+)\)", text):
        target = match.group(1).split("#")[0].strip()
        if target and "://" not in target and not target.startswith("mailto:"):
            yield target


if __name__ == "__main__":
    unittest.main()


class TestTheRunnerDocument(unittest.TestCase):
    """RUNNER.md restates the layout and the loop; both are code."""

    def setUp(self):
        from job_manager import remote_runner

        self.runner = remote_runner
        self.text = read(DOCS / "RUNNER.md")

    def layout(self) -> str:
        """The fenced directory listing, which names real files."""
        return self.text.split("<remote_root>/.moleditpy_runner/")[1].split("```")[1]

    def test_it_is_linked_from_the_readme_and_the_architecture(self):
        self.assertIn("docs/RUNNER.md", read(README))
        self.assertIn("RUNNER.md", read(DOCS / "ARCHITECTURE.md"))

    def test_every_directory_it_names_is_real(self):
        import re

        named = set(re.findall(r"^(\w+)/", self.layout(), re.M))
        # lock/ is made by ensure_runner rather than by prepare.
        self.assertEqual(named, set(self.runner.SUBDIRS) | {"lock"})

    def test_every_control_file_it_names_is_real(self):
        import re

        named = set(" ".join(re.findall(r"^([a-z][a-z. ]+)$", self.layout(), re.M)).split())
        self.assertEqual(
            named,
            {
                self.runner.SLOTS_NAME,
                self.runner.CORES_NAME,
                self.runner.MEMORY_NAME,
                self.runner.PAUSED_NAME,
                self.runner.SEQUENCE_NAME,
                self.runner.VERSION_NAME,
            },
        )

    def test_the_header_tags_are_quoted_verbatim(self):
        for tag in (
            self.runner.CORES_TAG,
            self.runner.MEMORY_TAG,
            self.runner.AFTER_TAG,
            self.runner.REQUIRE_SUCCESS_TAG,
        ):
            self.assertIn(tag, self.text, tag)

    def test_the_loop_it_quotes_is_the_loop_that_is_generated(self):
        script = self.runner.build_runner_script("/x")
        loop = script[script.index("while :; do") :]
        for line in ("reap", "dispatch", "rm -rf lock", "mkdir lock 2>/dev/null || exit 0"):
            self.assertIn(line, loop, f"generated: {line}")
            self.assertIn(line, self.text, f"documented: {line}")

    def test_the_poll_interval_matches(self):
        self.assertIn(f"sleep {self.runner.RUNNER_POLL_SECONDS}", self.text)

    def test_it_does_not_still_claim_a_fixed_runner_script_name(self):
        # The name is version-addressed now; documenting the old one would
        # send a user looking for a file that is not there.
        self.assertIn("moleditpy_runner_v<version>.sh", self.text)
        self.assertNotIn("moleditpy_runner.sh`", self.text)

    def test_it_explains_why_the_name_carries_both_halves(self):
        # The obvious question about job_0007_<id>: the number orders the
        # queue, the id identifies the job, and neither can do the other's job.
        entry = self.runner.entry_name(7, "a1b2c3d4e5f6")
        self.assertIn(entry, self.text)
        self.assertEqual(self.runner.parse_entry(entry), (7, "a1b2c3d4e5f6"))
        self.assertIn("dispatch number", self.text)
        self.assertIn("job id", self.text)

    def test_it_says_the_job_directory_is_never_deleted(self):
        # The practical consequence -- remote disk is never reclaimed -- is
        # something a user with a quota needs told, not left to infer.
        self.assertIn("never deleted", self.text)
        for path in ("docs/WORKFLOW.md",):
            self.assertIn("never deletes anything on a host", read(ROOT / path))
