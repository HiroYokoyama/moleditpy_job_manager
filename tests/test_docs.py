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

from job_manager import poller, runner, store
from job_manager.command_templates import TEMPLATES
from job_manager.models import SubmitPreset
from job_manager.schedulers import get_scheduler

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"


def read(path):
    return path.read_text(encoding="utf-8")


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
        minutes = poller.MAX_BACKOFF // 60
        self.assertIn(f"{minutes} minutes", read(DOCS / "WORKFLOW.md"))
        self.assertIn(f"{minutes} minutes", read(DOCS / "ARCHITECTURE.md"))

    def test_the_manual_refresh_cooldown(self):
        seconds = int(poller.MANUAL_REFRESH_COOLDOWN)
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
