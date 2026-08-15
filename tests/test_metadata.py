"""Plugin metadata and import hygiene.

Deliberately imports nothing but the package root, which must stay free of Qt
and of any third-party dependency so the host can read its metadata cheaply and
so this file passes on a bare ``pip install pytest``.
"""

import os
import re
import subprocess
import sys
import unittest

import job_manager

PACKAGE_DIR = os.path.dirname(os.path.abspath(job_manager.__file__))


class TestMetadata(unittest.TestCase):
    def test_required_fields_exist(self):
        for name in (
            "PLUGIN_NAME",
            "PLUGIN_VERSION",
            "PLUGIN_AUTHOR",
            "PLUGIN_DESCRIPTION",
            "PLUGIN_CATEGORY",
            "PLUGIN_TAGS",
            "PLUGIN_DEPENDENCIES",
            "PLUGIN_SUPPORTED_MOLEDITPY_VERSION",
            "PLUGIN_SUPPORTED_OS",
        ):
            self.assertTrue(hasattr(job_manager, name), f"missing {name}")

    def test_version_is_semver(self):
        self.assertRegex(job_manager.PLUGIN_VERSION, r"^\d+\.\d+\.\d+$")

    def test_name_and_author(self):
        self.assertEqual(job_manager.PLUGIN_NAME, "Job Manager")
        self.assertEqual(job_manager.PLUGIN_AUTHOR, "HiroYokoyama")

    def test_tags_are_a_non_empty_list_of_strings(self):
        self.assertTrue(job_manager.PLUGIN_TAGS)
        self.assertTrue(all(isinstance(t, str) for t in job_manager.PLUGIN_TAGS))

    def test_supported_os_covers_every_platform_openssh_ships_on(self):
        self.assertEqual(
            set(job_manager.PLUGIN_SUPPORTED_OS),
            {"Windows", "macOS", "Linux", "WSL"},
        )

    def test_paramiko_is_not_a_hard_dependency(self):
        # The default backend uses the system ssh client; forcing paramiko on
        # everyone would be gratuitous.
        self.assertEqual(job_manager.PLUGIN_DEPENDENCIES, [])

    def test_paramiko_is_declared_optional(self):
        # The registry carries this through to the Plugin Installer, which
        # lists an optional dependency and offers the pip command without ever
        # blocking an install on it.
        self.assertEqual(job_manager.PLUGIN_OPTIONAL_DEPENDENCIES, ["paramiko"])

    def test_the_description_says_what_the_optional_one_buys(self):
        # The Installer's optional section points at the description for what
        # the package is for, so the description has to answer that.
        self.assertIn("paramiko", job_manager.PLUGIN_DESCRIPTION)

    def test_supported_app_version_range(self):
        self.assertIn(">=4.0.0", job_manager.PLUGIN_SUPPORTED_MOLEDITPY_VERSION)

    def test_entry_points_exist(self):
        self.assertTrue(callable(job_manager.initialize))
        self.assertTrue(callable(job_manager.run))


class TestImportHygiene(unittest.TestCase):
    def test_the_package_root_does_not_import_qt(self):
        source = open(os.path.join(PACKAGE_DIR, "__init__.py"), encoding="utf-8").read()
        self.assertNotIn("from PyQt6", source.split("def ")[0])

    def test_pure_python_modules_stay_qt_free(self):
        # These are the modules the headless suite relies on.
        for name in ("models", "store", "runner", "remote_paths"):
            source = open(os.path.join(PACKAGE_DIR, f"{name}.py"), encoding="utf-8").read()
            self.assertNotIn("PyQt6", source, f"{name}.py must not import Qt")

    def test_transport_and_schedulers_stay_qt_free(self):
        for folder in ("transport", "schedulers"):
            directory = os.path.join(PACKAGE_DIR, folder)
            for filename in os.listdir(directory):
                if not filename.endswith(".py"):
                    continue
                source = open(os.path.join(directory, filename), encoding="utf-8").read()
                self.assertNotIn("PyQt6", source, f"{folder}/{filename}")

    def test_no_module_imports_paramiko_unguarded(self):
        pattern = re.compile(r"^import paramiko", re.MULTILINE)
        for root, _dirs, files in os.walk(PACKAGE_DIR):
            if "__pycache__" in root:
                continue
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(root, filename)
                source = open(path, encoding="utf-8").read()
                if not pattern.search(source):
                    continue
                self.assertIn("except (ImportError", source, path)

    def test_importing_the_root_does_not_pull_in_qt(self):
        # Reading metadata must not cost a Qt import; the host does this for
        # every installed plugin at startup. Checked in a subprocess: purging
        # job_manager from this interpreter's sys.modules would break the
        # patch targets of every test module that runs after this one.
        repo_root = os.path.dirname(PACKAGE_DIR)
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import job_manager;"
            "sys.exit(1 if 'PyQt6' in sys.modules else 0)" % repo_root
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True)
        self.assertEqual(result.returncode, 0, "importing job_manager pulled in PyQt6")


class TestDataLocation(unittest.TestCase):
    def test_nothing_is_persisted_inside_the_package(self):
        # The Plugin Installer replaces the package folder on update.
        leftovers = [
            name for name in os.listdir(PACKAGE_DIR) if name in ("jobs.json", "settings.json")
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
