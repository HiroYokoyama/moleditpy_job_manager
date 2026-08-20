"""Where the chat webhook URL is typed, and tried.

A wrong URL is otherwise only discovered hours later when a job ends and
nothing arrives, so the test message is part of the dialog itself.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThreadPool
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import webhook
from .tasks import run_async

PLACEHOLDER = "https://hooks.slack.com/services/..."

EXPLANATION = (
    "Post a line to a chat room when a job ends, as well as to this desktop. "
    "Paste an incoming-webhook URL created in your own workspace -- Slack, "
    "Discord, Teams, or anything else that accepts a JSON POST. The job's name "
    "and the host it ran on are what gets sent. Empty means nothing is sent."
)


TEST_MESSAGE = "Test message. Job alerts will arrive here."


class ChatWebhookDialog(QDialog):
    """Edit, test and save the webhook a finished job is announced to."""

    def __init__(self, store, parent: Optional[QWidget] = None, pool=None) -> None:
        super().__init__(parent)
        self.store = store
        self.pool = pool or QThreadPool.globalInstance()
        self.setWindowTitle("Chat alerts")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        blurb = QLabel(EXPLANATION)
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self.edit_url = QLineEdit(str(store.get_pref("notify_webhook", "") or ""))
        self.edit_url.setPlaceholderText(PLACEHOLDER)
        self.edit_url.textChanged.connect(self._on_url_changed)
        layout.addWidget(self.edit_url)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.lbl_status)

        self.btn_test = QPushButton("Send a test message")
        self.btn_test.clicked.connect(self._send_test)
        layout.addWidget(self.btn_test)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._on_url_changed(self.edit_url.text())

    # --- validation ---------------------------------------------------------

    def _on_url_changed(self, text: str) -> None:
        url = (text or "").strip()
        if not url:
            self._say("Nothing is posted while this is empty.")
            self.btn_test.setEnabled(False)
            return
        if not webhook.is_supported(url):
            self._say("That is not an http or https URL.", error=True)
            self.btn_test.setEnabled(False)
            return
        self._say(f"Recognised as {webhook.service_name(url)}.")
        self.btn_test.setEnabled(True)

    def _say(self, message: str, error: bool = False) -> None:
        self.lbl_status.setText(message)
        self.lbl_status.setStyleSheet("color: #d1242f;" if error else "")

    # --- the test -----------------------------------------------------------

    def _send_test(self) -> None:
        """Post off the GUI thread: this dialog is modal, and a chat service
        that has gone away would otherwise freeze it behind an unclickable
        window."""
        url = self.edit_url.text().strip()
        if not webhook.is_supported(url):
            return
        self.btn_test.setEnabled(False)
        self._say("Sending...")
        run_async(
            self.pool,
            webhook.post,
            self._on_test_done,
            None,
            None,
            url,
            "MoleditPy job manager",
            TEST_MESSAGE,
            quiet=True,
        )

    def _on_test_done(self, ok: bool) -> None:
        self.btn_test.setEnabled(True)
        if ok:
            self._say("Sent. It should be in the room now.")
        else:
            self._say(
                "The service did not accept it. Check the URL, and that the "
                "webhook still exists in the workspace.",
                error=True,
            )

    # --- saving -------------------------------------------------------------

    def accept(self) -> None:
        # Saved even when it is not a URL at all: refusing to close on a typo
        # would trap someone who was just clearing the field.
        url = self.edit_url.text().strip()
        self.store.set_pref("notify_webhook", url)
        # Saving a URL never switches posting on by itself -- that is the
        # tick's decision. Clearing the URL does switch it off.
        if not url:
            self.store.set_pref("notify_chat", False)
        super().accept()


__all__ = ["ChatWebhookDialog"]
