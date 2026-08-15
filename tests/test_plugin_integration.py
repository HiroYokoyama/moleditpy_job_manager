"""Contract with the main application's PluginContext.

Two tiers:

1. **Stub mode** (always runs, CI included): a StubPluginContext mirroring the
   documented V4 API records what ``initialize`` registers.
2. **Real-context mode** (runs when the main app source is present, locally as
   a sibling checkout or in CI via ``CI_MAIN_APP_SRC``): the same calls are
   made against the genuine ``PluginContext`` class.

This file imports nothing from Qt, so it runs on a bare ``pip install pytest``.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

import job_manager

_MAIN_APP_CANDIDATES = [
    os.path.normpath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "python_molecular_editor", "moleditpy", "src"
        )
    ),
    os.environ.get("CI_MAIN_APP_SRC", ""),
]
_MAIN_APP_SRC = next((p for p in _MAIN_APP_CANDIDATES if p and os.path.isdir(p)), None)
HAS_MAIN_APP = _MAIN_APP_SRC is not None

#: Every PluginContext member this plugin touches, anywhere in its source.
USED_CONTEXT_MEMBERS = (
    "add_menu_action",
    "register_drop_handler",
    "get_window",
    "register_window",
    "get_main_window",
    "show_status_message",
)


class StubPluginContext:
    """Minimal stand-in mirroring the documented V4 surface."""

    def __init__(self):
        self.menu_actions = []
        self.drop_handlers = []
        self.windows = {}
        self.status_messages = []
        self.main_window = MagicMock()

    def add_menu_action(self, path, callback, text=None, icon=None, shortcut=None):
        self.menu_actions.append((path, callback))

    def register_drop_handler(self, callback, priority=0):
        self.drop_handlers.append((callback, priority))

    def add_plugin_menu(self, path, callback, text=None, icon=None, shortcut=None):
        # Present because the host has it, and asserted against: these entries
        # belong under Extensions, which this method cannot reach.
        raise AssertionError("add_plugin_menu is hard-wired to the Plugin menu")

    def register_window(self, window_id, window):
        self.windows[window_id] = window

    def get_window(self, window_id):
        return self.windows.get(window_id)

    def get_main_window(self):
        return self.main_window

    def show_status_message(self, message, timeout=3000):
        self.status_messages.append(message)


class TestInitializeContract(unittest.TestCase):
    def setUp(self):
        self.context = StubPluginContext()
        job_manager.initialize(self.context)
        self.addCleanup(setattr, job_manager, "_context", None)

    def test_menu_actions_are_registered(self):
        self.assertEqual(len(self.context.menu_actions), 3)

    def test_actions_live_under_the_extensions_menu(self):
        # A top-level menu of its own, which the host creates on demand --
        # see plugin_menu_manager.add_registered_plugin_actions.
        for path, _callback in self.context.menu_actions:
            self.assertTrue(path.startswith("Extensions/"), path)

    def test_callbacks_are_callable(self):
        for _path, callback in self.context.menu_actions:
            self.assertTrue(callable(callback))

    def test_initialize_touches_nothing_else(self):
        # No window is created and no molecule is read at load time.
        self.assertEqual(self.context.windows, {})
        self.assertEqual(self.context.status_messages, [])

    def test_no_project_handlers_are_registered(self):
        # Jobs are global by design: they outlive the open .pmeprj.
        self.assertFalse(hasattr(self.context, "save_handlers"))
        for name in ("register_save_handler", "register_load_handler"):
            self.assertFalse(
                hasattr(self.context, name),
                f"the stub does not offer {name}; initialize must not need it",
            )

    def test_forget_window_uses_the_registry(self):
        job_manager.forget_window()
        self.assertIsNone(self.context.get_window(job_manager.WINDOW_KEY))


class TestNoHeavyImportsAtLoad(unittest.TestCase):
    def test_initialize_does_not_import_qt_or_paramiko(self):
        before = {name for name in sys.modules if name.split(".")[0] in ("PyQt6", "paramiko")}
        job_manager.initialize(StubPluginContext())
        after = {name for name in sys.modules if name.split(".")[0] in ("PyQt6", "paramiko")}
        self.assertEqual(before, after)


try:
    import pytest

    _skipif = pytest.mark.skipif(
        not HAS_MAIN_APP,
        reason="main app not found; set CI_MAIN_APP_SRC or place it at "
        "../python_molecular_editor/moleditpy/src",
    )
except ImportError:  # pragma: no cover

    def _skipif(cls):
        return unittest.skip("pytest not available for skipif")(cls)


@_skipif
class TestWithRealPluginContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAS_MAIN_APP:
            return
        if _MAIN_APP_SRC not in sys.path:
            sys.path.insert(0, _MAIN_APP_SRC)
        try:
            from moleditpy.plugins.plugin_interface import PluginContext
        except ImportError as exc:
            # `import moleditpy` pulls in PyQt6 via utils/constants, so a
            # checkout of the main app is not on its own enough to run this
            # tier. Skip rather than error when its dependencies are absent.
            raise unittest.SkipTest(f"main app present but not importable: {exc}") from exc

        cls.PluginContext = PluginContext
        manager = MagicMock()
        manager.get_main_window.return_value = MagicMock()
        cls.real_context = PluginContext(manager, job_manager.PLUGIN_NAME)

    def test_initialize_against_the_real_context(self):
        try:
            job_manager.initialize(self.real_context)
        except Exception as exc:
            self.fail(f"initialize(real_context) raised: {exc}")

    def test_every_member_we_use_exists(self):
        for name in USED_CONTEXT_MEMBERS:
            self.assertTrue(
                hasattr(self.PluginContext, name),
                f"Real PluginContext is missing {name}",
            )

    def test_the_stub_matches_the_real_signature_set(self):
        for name in USED_CONTEXT_MEMBERS:
            self.assertTrue(hasattr(StubPluginContext, name), name)

    def test_the_result_handoff_target_still_exists(self):
        # jobs_dialog.open_in_host() calls this to reuse the host's registered
        # file openers instead of hard-coding analyzer plugins.
        #
        # Checked by parsing rather than importing: main_window_init pulls in
        # pyvistaqt/vtk, and requiring the whole 3D stack just to confirm a
        # method name would make this tier too expensive to keep running.
        import ast

        source_path = os.path.join(_MAIN_APP_SRC, "moleditpy", "ui", "main_window_init.py")
        self.assertTrue(os.path.isfile(source_path), source_path)
        with open(source_path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        methods = {
            node.name
            for cls in tree.body
            if isinstance(cls, ast.ClassDef)
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("load_command_line_file", methods)


if __name__ == "__main__":
    unittest.main()
