"""A host switched off in the Hosts dialog is not offered for submission."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from job_manager.submit_dialog import SubmitDialog  # noqa: E402

from .fakes import make_host  # noqa: E402
from .test_dialogs import DialogTestCase  # noqa: E402


class TestSubmitSkipsDisabledHosts(DialogTestCase):
    def add_disabled(self):
        return self.store.add_host(make_host(id="off", name="maintenance", enabled=False))

    def hosts_offered(self, dialog):
        return [dialog.cmb_host.itemData(i) for i in range(dialog.cmb_host.count())]

    def test_a_disabled_host_is_not_listed(self):
        self.add_disabled()
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        self.assertEqual(self.hosts_offered(dialog), [self.host.id])

    def test_the_last_used_host_is_not_reopened_once_disabled(self):
        disabled = self.add_disabled()
        self.store.set_pref("last_host_id", disabled.id)
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.current_host().id, self.host.id)

    def test_a_resubmit_to_a_disabled_host_does_not_select_it(self):
        disabled = self.add_disabled()
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        dialog.prefill(host_id=disabled.id)
        self.assertEqual(dialog.current_host().id, self.host.id)

    def test_every_host_disabled_says_so(self):
        self.host.enabled = False
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        self.assertIsNone(dialog.current_host())
        with patch.object(QMessageBox, "warning") as warning:
            dialog._submit()
        self.assertIn("disabled", warning.call_args.args[2])
        self.assertEqual(self.store.jobs, {})

    def test_a_host_disabled_while_the_window_is_open_is_refused(self):
        dialog = SubmitDialog(self.service)
        self.addCleanup(dialog.close)
        self.host.enabled = False
        with patch.object(QMessageBox, "warning") as warning:
            dialog._submit()
        self.assertIn("disabled", warning.call_args.args[2])
        self.assertEqual(self.store.jobs, {})
