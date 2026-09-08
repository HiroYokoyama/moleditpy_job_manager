"""Start or stop the read-only web view, and hand over the two things needed
to reach it from somewhere else: the URL, and the ``tailscale serve`` command
that publishes it to the tailnet.

The command is shown rather than run. Serving a machine onto a network is the
user's decision to make with their own tailnet's ACLs in view, and a plugin
that silently ran it would be making that decision for them.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION
from .web_monitor import tailscale_available, tailscale_command


class _CopyRow(QWidget):
    """A read-only field with a Copy button, for something meant to be pasted."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.field = QLineEdit(text)
        self.field.setReadOnly(True)
        # Selectable but not editable: someone will try to select it by hand
        # anyway, and a field that looks typeable but silently discards the
        # typing is worse than one that is plainly read-only.
        layout.addWidget(self.field, 1)
        self.button = QPushButton("Copy")
        self.button.clicked.connect(self._copy)
        layout.addWidget(self.button)

    def set_text(self, text: str) -> None:
        self.field.setText(text)

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.field.text())
        self.button.setText("Copied")
        # Reverts on the next paint the user causes; a timer here would be one
        # more thing to cancel when the dialog closes under it.
        self.button.setToolTip(self.field.text())


class WebMonitorDialog(QDialog):
    """Reached from the Host Monitor's "Web..." button."""

    def __init__(self, monitor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.monitor = monitor
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Web Monitor")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        self.lbl_state = QLabel()
        self.lbl_state.setWordWrap(True)
        layout.addWidget(self.lbl_state)

        layout.addWidget(QLabel("On this machine:"))
        self.row_url = _CopyRow()
        layout.addWidget(self.row_url)

        hint = QLabel(
            "From a phone or another machine, publish it to your tailnet with "
            "Tailscale. The page stays bound to 127.0.0.1 -- Tailscale is what "
            "carries it, with its own identity check in front:"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.row_cmd = _CopyRow()
        layout.addWidget(self.row_cmd)

        self.lbl_tailnet = QLabel()
        self.lbl_tailnet.setWordWrap(True)
        layout.addWidget(self.lbl_tailnet)

        self.lbl_note = QLabel(
            "The view is read-only: it shows hosts and job states, and has no "
            "route that cancels, submits or downloads anything."
        )
        self.lbl_note.setWordWrap(True)
        layout.addWidget(self.lbl_note)

        buttons = QHBoxLayout()
        self.btn_toggle = QPushButton()
        self.btn_toggle.clicked.connect(self._toggle)
        buttons.addWidget(self.btn_toggle)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._refresh()

    # ------------------------------------------------------------------

    def _server(self):
        return getattr(self.monitor, "_web", None)

    def _running(self) -> bool:
        server = self._server()
        return server is not None and server.running

    def _toggle(self) -> None:
        if self._running():
            self.monitor._stop_web()
        else:
            self.monitor._start_web()
        self._refresh()

    def _refresh(self) -> None:
        server = self._server()
        if self._running():
            port = server.port
            self.lbl_state.setText(f"Serving on 127.0.0.1:{port}, read-only.")
            self.row_url.set_text(server.url())
            self.row_cmd.set_text(tailscale_command(port))
            self.lbl_tailnet.setText(
                "Then open: " + server.tailscale_url() + "\n"
                "(the token is in the link, and is stored as a cookie after the "
                "first load)"
            )
            self.btn_toggle.setText("Stop serving")
        else:
            self.lbl_state.setText("Not serving. Nothing is listening.")
            self.row_url.set_text("")
            self.row_cmd.set_text("")
            self.lbl_tailnet.setText("")
            self.btn_toggle.setText("Start serving")
        for row in (self.row_url, self.row_cmd):
            row.setEnabled(self._running())
            row.button.setText("Copy")
        if not tailscale_available():
            self.lbl_tailnet.setText(
                (self.lbl_tailnet.text() + "\n\n" if self._running() else "")
                + "Tailscale was not found on PATH. The command above still "
                "applies once it is installed; nothing here needs it to serve "
                "on this machine."
            )


__all__ = ["WebMonitorDialog"]
