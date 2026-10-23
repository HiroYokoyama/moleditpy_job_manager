import unittest
import importlib.util
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TESTS_DIR.parent
_WORKSPACE_ROOT = _PLUGIN_ROOT.parent

_DEFAULT_APP_CANDIDATES = [
    _WORKSPACE_ROOT / "python_molecular_editor",
    _PLUGIN_ROOT / "python_molecular_editor",  # CI path
]
_APP_PATH = next((p for p in _DEFAULT_APP_CANDIDATES if p and (p / "moleditpy").exists()), None)
HAS_APP = _APP_PATH is not None


# ---------------------------------------------------------------------------
# Load plugin_api_checker (always present in the repo)
# ---------------------------------------------------------------------------


def _load_checker():
    checker_path = _TESTS_DIR / "plugin_api_checker.py"
    spec = importlib.util.spec_from_file_location("plugin_api_checker", checker_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(checker_path)
    spec.loader.exec_module(mod)
    return mod


_checker = _load_checker()
Issue = _checker.Issue
APIInfo = _checker.APIInfo
AppAPIExtractor = _checker.AppAPIExtractor
PluginFileChecker = _checker.PluginFileChecker
_merge_allowlists = _checker._merge_allowlists
_load_site_allowlist = _checker._load_site_allowlist


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestIssue
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestMergeAllowlists
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestLoadSiteAllowlist
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestPluginFileChecker  (synthetic code — no main app required)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestAppAPIExtractor  (synthetic app tree in temp dir — no main app required)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestAPIChecker — integration test (skipped when main app is not present)
# ---------------------------------------------------------------------------


class TestAPIChecker(unittest.TestCase):
    @unittest.skipUnless(HAS_APP, "Main application repository (python_molecular_editor) not found")
    def test_no_unknown_api_accesses(self):
        checker_mod = _load_checker()

        extractor = checker_mod.AppAPIExtractor(_APP_PATH, verbose=False)
        api = extractor.extract()

        site_allowlist = checker_mod._load_site_allowlist(_PLUGIN_ROOT)
        allowlist = checker_mod._merge_allowlists(
            checker_mod._MANAGER_ALLOWLIST, checker_mod._MW_ALLOWLIST, site_allowlist
        )

        plugin_files = []
        for p in _PLUGIN_ROOT.rglob("*.py"):
            if (
                "tests" in p.parts
                or any(part.startswith(".") for part in p.parts)
                or "__pycache__" in p.parts
            ):
                continue
            if p.name in ("test_api.py", "plugin_api_checker.py"):
                continue
            plugin_files.append(p)

        all_issues = []
        for pf in plugin_files:
            checker = checker_mod.PluginFileChecker(
                pf, api, check_context=True, allowlist=allowlist
            )
            issues = checker.check()
            if issues:
                all_issues.extend(issues)

        if all_issues:
            lines = [
                f"  [{i.code}] {Path(i.file).relative_to(_PLUGIN_ROOT)} line {i.line}: {i.message}"
                for i in all_issues
            ]
            self.fail(
                f"{len(all_issues)} unknown API access(es) found in {Path(_PLUGIN_ROOT).name}:\n"
                + "\n".join(lines)
            )


if __name__ == "__main__":
    unittest.main()
