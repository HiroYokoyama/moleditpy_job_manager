"""Every preference that is not part of one submission, in one window.

They used to be wherever they were added: a row of ticks under the job table
that pushed the window's minimum width past a laptop screen, the poll interval
in the toolbar, three switches only in the tray icon's menu -- which Windows
hides behind the ``^`` until it is pinned -- and the local API behind its own
menu entry. Nobody could say where to look. Now there is one place.

Each control writes its preference as it changes, as the scattered ones did:
there is nothing to apply and nothing lost by closing the window.

Per-submission choices (which host, auto-download for this job) stay in the
wizard, where they are made. The download location is here as well, since it
is a standing default the wizard only shows.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION, webhook, win_taskbar
from .store import MAX_POLL_INTERVAL, MIN_POLL_INTERVAL, RECOMMENDED_MIN_POLL_INTERVAL
from .theme import CY_AMBER, apply_theme

KEEP_TRACKING_TEXT = "Keep tracking jobs after MoleditPy closes"
ONLY_IF_OPENED_TEXT = "Only if the Job Manager was opened in that session"


class SettingsDialog(QDialog):
    """Polling, results, being told, the desktop, and the local API."""

    def __init__(self, service, parent: Optional[QWidget] = None, standalone: bool = False):
        super().__init__(parent)
        self.service = service
        self.store = service.store
        #: In the tray process MoleditPy started: no API to serve from here.
        self.standalone = standalone
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Settings")
        apply_theme(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self._polling_group())
        layout.addWidget(self._results_group())
        layout.addWidget(self._job_end_group())
        layout.addWidget(self._desktop_group())
        if not standalone:
            layout.addWidget(self._api_group())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._update_interval_warning()
        self._sync_chat_controls()
        self._sync_api_status()

    # --- the groups ----------------------------------------------------------

    def _polling_group(self) -> QGroupBox:
        group = QGroupBox("Polling")
        row = QHBoxLayout(group)
        row.addWidget(QLabel("Ask each host for its queue every"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL)
        self.spin_interval.setSingleStep(30)
        self.spin_interval.setSuffix(" s")
        self.spin_interval.setValue(self.store.poll_interval)
        self.spin_interval.setToolTip(
            "One status query per host per cycle. "
            f"{RECOMMENDED_MIN_POLL_INTERVAL} s or slower is the courteous setting."
        )
        self.spin_interval.valueChanged.connect(self._on_interval_changed)
        row.addWidget(self.spin_interval)
        self.lbl_interval_warning = QLabel("")
        self.lbl_interval_warning.setStyleSheet(f"color: {CY_AMBER};")
        row.addWidget(self.lbl_interval_warning)
        row.addStretch(1)
        return group

    def _results_group(self) -> QGroupBox:
        group = QGroupBox("Results")
        form = QFormLayout(group)
        self.chk_auto_open = self._tick(
            "Open results in MoleditPy when they arrive", "open_result_after_download", True
        )
        form.addRow(self.chk_auto_open)
        self.chk_beside_input = self._tick(
            "Save results beside the input file", "download_beside_input", True
        )
        self.chk_beside_input.setToolTip(
            "Otherwise they go to the download folder below, one folder per job."
        )
        form.addRow(self.chk_beside_input)
        folder_row = QHBoxLayout()
        self.txt_download_root = QLineEdit(str(self.store.get_pref("download_root", "") or ""))
        self.txt_download_root.setPlaceholderText(self.store.download_root())
        # editingFinished, not textChanged: a preference write fsyncs, and
        # textChanged would fire one per keystroke.
        self.txt_download_root.editingFinished.connect(self._save_download_root)
        folder_row.addWidget(self.txt_download_root, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_download_root)
        folder_row.addWidget(browse)
        form.addRow("Download folder", folder_row)
        return group

    def _job_end_group(self) -> QGroupBox:
        group = QGroupBox("When a job ends")
        form = QFormLayout(group)
        self.chk_notify = self._tick(
            "Show a desktop notification", "notify_on_finish", True, refresh=False
        )
        form.addRow(self.chk_notify)
        self.chk_flash = self._tick(
            "Flash the task bar button (bounce the Dock icon on macOS)",
            "flash_on_finish",
            True,
            refresh=False,
        )
        form.addRow(self.chk_flash)
        chat_row = QHBoxLayout()
        self.chk_chat = QCheckBox("Post to a chat room")
        self.chk_chat.toggled.connect(
            lambda checked: self.store.set_pref("notify_chat", bool(checked))
        )
        chat_row.addWidget(self.chk_chat)
        self.btn_chat = QPushButton("Chat webhook...")
        self.btn_chat.setToolTip(
            "Slack, Discord or Teams: the news reaches you away from this machine."
        )
        self.btn_chat.clicked.connect(self._edit_chat_webhook)
        chat_row.addWidget(self.btn_chat)
        chat_row.addStretch(1)
        form.addRow(chat_row)
        return group

    def _desktop_group(self) -> QGroupBox:
        group = QGroupBox("Desktop")
        form = QFormLayout(group)
        self.chk_taskbar_badge = self._tick(
            "Show the job count on MoleditPy's own icon", "taskbar_badge", False
        )
        self.chk_taskbar_badge.setToolTip(
            "The Dock, the task bar button or the launcher entry. On Windows the "
            "batch's progress goes on MoleditPy's task bar button too."
        )
        self.chk_taskbar_badge.toggled.connect(self._on_badge_toggled)
        form.addRow(self.chk_taskbar_badge)
        self.chk_taskbar_progress = self._tick(
            "Show progress on the Job Monitor's task bar button", "taskbar_progress", True
        )
        # Windows only: elsewhere the tick would change nothing anyone can see.
        self.chk_taskbar_progress.setVisible(win_taskbar.AVAILABLE)
        form.addRow(self.chk_taskbar_progress)
        self.chk_keep_tracking = self._tick(
            KEEP_TRACKING_TEXT, "keep_running_in_tray", False, refresh=False
        )
        self.chk_keep_tracking.setToolTip(
            "When MoleditPy closes with jobs still active, the Job Manager carries on "
            "by itself in the tray -- polling, notifying and downloading -- until "
            "MoleditPy is opened again."
        )
        self.chk_keep_tracking.toggled.connect(self._on_keep_tracking_toggled)
        form.addRow(self.chk_keep_tracking)
        self.chk_only_if_opened = self._tick(
            ONLY_IF_OPENED_TEXT, "keep_running_only_if_opened", True, refresh=False
        )
        self.chk_only_if_opened.setToolTip(
            "When the Job Manager was never opened while MoleditPy ran, closing "
            "MoleditPy closes it too, even with jobs still active."
        )
        self.chk_only_if_opened.setEnabled(self.chk_keep_tracking.isChecked())
        self.chk_keep_tracking.toggled.connect(self.chk_only_if_opened.setEnabled)
        self.chk_only_if_opened.toggled.connect(self._on_keep_tracking_toggled)
        form.addRow(self.chk_only_if_opened)
        return group

    def _api_group(self) -> QGroupBox:
        group = QGroupBox("Local API")
        row = QHBoxLayout(group)
        self.lbl_api = QLabel("")
        row.addWidget(self.lbl_api, 1)
        self.btn_api = QPushButton("Local API...")
        self.btn_api.clicked.connect(self._open_api_dialog)
        row.addWidget(self.btn_api)
        return group

    # --- behaviour -------------------------------------------------------------

    def _tick(self, text: str, pref: str, default: bool, refresh: bool = True) -> QCheckBox:
        box = QCheckBox(text)
        box.setChecked(bool(self.store.get_pref(pref, default)))

        def save(checked: bool) -> None:
            self.store.set_pref(pref, bool(checked))
            if refresh:
                # The status bar, the badge and the task bar redraw on this.
                self.service.jobs_changed.emit()

        box.toggled.connect(save)
        return box

    def _on_interval_changed(self, value: int) -> None:
        self.store.set_pref("poll_interval", int(value))
        self.service.poller.reschedule()
        self._update_interval_warning()

    def _update_interval_warning(self) -> None:
        """Fast polling is permitted, but never silent."""
        if not self.store.poll_interval_is_aggressive:
            self.lbl_interval_warning.setText("")
            self.lbl_interval_warning.setToolTip("")
            return
        self.lbl_interval_warning.setText("fast polling")
        self.lbl_interval_warning.setToolTip(
            f"Faster than {RECOMMENDED_MIN_POLL_INTERVAL} s queries the login node every "
            "few seconds, for every host you have jobs on."
        )

    def _save_download_root(self) -> None:
        self.store.set_pref("download_root", self.txt_download_root.text().strip())

    def _browse_download_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Download Folder", self.store.download_root())
        if path:
            self.txt_download_root.setText(path)
            self._save_download_root()

    def _on_badge_toggled(self, enabled: bool) -> None:
        # Cleared now, not at the next poll, so switching off takes it away.
        if not enabled:
            from .taskbar import clear_badge

            clear_badge()

    def _on_keep_tracking_toggled(self, _enabled: bool) -> None:
        from . import presence

        current = presence.current()
        if current is not None and current.tray is not None:
            current.tray.apply_keep_running()

    def _edit_chat_webhook(self) -> None:
        from .chat_webhook_dialog import ChatWebhookDialog

        ChatWebhookDialog(self.store, self, pool=self.service.pool).exec()
        self._sync_chat_controls()

    def _sync_chat_controls(self) -> None:
        """Show the tick as unusable until a room is configured -- a tick set
        with no webhook behind it would claim to post when nothing is sent."""
        url = str(self.store.get_pref("notify_webhook", "") or "")
        # Blocked: setChecked here must not overwrite the user's own setting
        # with the merely-displayed state every time the window opens.
        self.chk_chat.blockSignals(True)
        self.chk_chat.setChecked(bool(url) and bool(self.store.get_pref("notify_chat")))
        self.chk_chat.blockSignals(False)
        self.chk_chat.setEnabled(bool(url))
        self.chk_chat.setToolTip(
            f"Post to {webhook.service_name(url)} as well, when a job ends."
            if url
            else "Set a webhook URL under Chat webhook... first."
        )

    def _sync_api_status(self) -> None:
        if self.standalone:
            return
        try:
            from . import api_external_port, api_is_running, get_api_server

            if api_is_running():
                server = get_api_server(create=False)
                text = f"On, listening on 127.0.0.1:{server.port}"
            elif api_external_port():
                text = f"Served by another MoleditPy on port {api_external_port()}"
            elif self.store.get_pref("api_enabled", False):
                text = "On, but not listening"
            else:
                text = "Off: no other program can submit jobs"
        except Exception:
            logging.debug("Job Manager: API status unknown", exc_info=True)
            text = ""
        self.lbl_api.setText(text)

    def _open_api_dialog(self) -> None:
        from .api_dialog import ApiDialog

        ApiDialog(self.service, self).exec()
        self._sync_api_status()


__all__ = ["KEEP_TRACKING_TEXT", "ONLY_IF_OPENED_TEXT", "SettingsDialog"]
