"""The default star says when it takes effect, since by default it does not."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .fakes import make_preset  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TestDefaultStarWording(DialogTestCase):
    def open_on_preset(self):
        self.store.add_preset(make_preset(id="pb", name="B", host_id=self.host.id))
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        dialog.cmb_preset.setCurrentIndex(dialog.cmb_preset.findData("pb"))
        return dialog

    def test_the_tip_names_the_setting_it_needs(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        self.assertIn("New submission starts from", dialog.btn_default_preset.toolTip())
        self.assertIn("not set to that now", dialog.btn_default_preset.toolTip())

    def test_the_tip_says_it_is_used_once_settings_opens_on_it(self):
        self.store.set_pref("submit_start_from", "default")
        dialog = self.open_on_preset()
        tip = dialog.btn_default_preset.toolTip()
        self.assertIn("open on it", tip)
        self.assertNotIn("not set to that now", tip)

    def test_starring_without_the_setting_says_why_nothing_changes(self):
        dialog = self.open_on_preset()
        said = []
        self.service.message.connect(said.append)
        dialog.btn_default_preset.click()
        self.assertEqual(self.store.default_preset_id(self.host.id), "pb")
        self.assertEqual(len(said), 1)
        self.assertIn("B is testcluster's default preset", said[0])
        self.assertIn("New submission starts from", said[0])

    def test_starring_with_the_setting_says_nothing_extra(self):
        self.store.set_pref("submit_start_from", "default")
        dialog = self.open_on_preset()
        said = []
        self.service.message.connect(said.append)
        dialog.btn_default_preset.click()
        self.assertEqual(self.store.default_preset_id(self.host.id), "pb")
        self.assertEqual(said, [])

    def test_the_behaviour_is_unchanged(self):
        self.store.add_preset(make_preset(id="pb", name="B", host_id=self.host.id))
        self.store.set_default_preset(self.host.id, "pb")
        empty = SubmitDialog(self.service)
        self.addCleanup(empty.close)
        self.assertEqual(empty.cmb_preset.currentData(), "")
        self.store.set_pref("submit_start_from", "default")
        starred = SubmitDialog(self.service)
        self.addCleanup(starred.close)
        self.assertEqual(starred.cmb_preset.currentData(), "pb")
