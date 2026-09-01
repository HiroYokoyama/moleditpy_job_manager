"""The Local API window: the switch, the port, the token, and how to use it.

The API is off until someone opens this and turns it on, so this window is
also where the decision is explained -- what the token lets a program do, and
that it is a local socket and not a network service.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from . import api_core

_EXPLANATION = (
    "Let another program on this machine submit jobs, read their state and "
    "fetch results, through a small HTTP API on 127.0.0.1.\n\n"
    "It is off until you switch it on, and it never listens on a network "
    "address. A request must carry the token below; any program running as "
    "you can read that token, and can then submit to your clusters exactly as "
    "you could yourself."
)


class ApiDialog(QDialog):
    """Turn the local API on or off, and read the details a client needs."""

    def __init__(self, service: Any, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Job Manager - Local API")
        self._build()
        self._sync()

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        blurb = QLabel(_EXPLANATION)
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self.chk_enabled = QCheckBox("Allow local programs to submit jobs")
        self.chk_enabled.toggled.connect(self._on_toggled)
        layout.addWidget(self.chk_enabled)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1024, 65535)
        self.spin_port.setValue(int(self.service.store.get_pref("api_port", api_core.DEFAULT_PORT)))
        self.spin_port.setToolTip(
            "A port already in use is not an error: the API takes a free one "
            "instead, and clients that discover it are unaffected."
        )
        self.spin_port.valueChanged.connect(self._on_port_changed)
        port_row.addWidget(self.spin_port)
        port_row.addStretch(1)
        layout.addLayout(port_row)

        details = QGroupBox("For the client")
        details_layout = QVBoxLayout(details)

        self.txt_url = self._readonly_row(details_layout, "URL:")
        self.txt_token = self._readonly_row(details_layout, "Token:", secret=True)

        self.lbl_endpoint = QLabel("")
        self.lbl_endpoint.setWordWrap(True)
        self.lbl_endpoint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_layout.addWidget(self.lbl_endpoint)
        layout.addWidget(details)

        buttons_row = QHBoxLayout()
        self.btn_copy = QPushButton("Copy token")
        self.btn_copy.clicked.connect(self._copy_token)
        buttons_row.addWidget(self.btn_copy)
        self.btn_renew = QPushButton("New token")
        self.btn_renew.setToolTip(
            "Issue a new token. Every client using the old one stops working."
        )
        self.btn_renew.clicked.connect(self._renew)
        buttons_row.addWidget(self.btn_renew)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _readonly_row(self, layout: QVBoxLayout, label: str, secret: bool = False) -> QLineEdit:
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        field = QLineEdit()
        field.setReadOnly(True)
        if secret:
            # Shown on demand rather than by default: this window is the sort
            # of thing that ends up in a screen share.
            field.setEchoMode(QLineEdit.EchoMode.Password)
            reveal = QPushButton("Show")
            reveal.setCheckable(True)
            reveal.toggled.connect(
                lambda shown: field.setEchoMode(
                    QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
                )
            )
            row.addWidget(field, 1)
            row.addWidget(reveal)
        else:
            row.addWidget(field, 1)
        layout.addLayout(row)
        return field

    # --- state --------------------------------------------------------------

    def _server(self):
        from . import get_api_server

        return get_api_server()

    def _sync(self) -> None:
        from . import api_is_running

        running = api_is_running()
        # Blocked: the tick reflects the preference, and setting it here must
        # not run the toggle handler and stop the server it just reported.
        self.chk_enabled.blockSignals(True)
        self.chk_enabled.setChecked(bool(self.service.store.get_pref("api_enabled", False)))
        self.chk_enabled.blockSignals(False)

        server = self._server() if running else None
        self.txt_url.setText(server.url() if server else "")
        self.txt_token.setText(
            server.token() if server else api_core.read_token(self.service.store.directory)
        )
        for widget in (self.txt_url, self.txt_token, self.btn_copy, self.btn_renew):
            widget.setEnabled(running)
        self.lbl_endpoint.setText(
            f"A client finds these itself in {api_core.endpoint_path(self.service.store.directory)}"
            if running
            else "The endpoint file is written while the API is listening."
        )
        if running and server is not None:
            actual = server.port
            wanted = int(self.spin_port.value())
            self.lbl_status.setText(
                f"Listening on port {actual}."
                if actual == wanted
                else f"Listening on port {actual}: port {wanted} was already in use."
            )
        else:
            self.lbl_status.setText("Not listening.")

    def _on_toggled(self, enabled: bool) -> None:
        from . import start_api, stop_api

        self.service.store.set_pref("api_enabled", bool(enabled))
        if enabled:
            if not start_api(int(self.spin_port.value())):
                self.lbl_status.setText("The API could not start; see the application log.")
        else:
            stop_api()
        self._sync()

    def _on_port_changed(self, port: int) -> None:
        from . import api_is_running, start_api, stop_api

        self.service.store.set_pref("api_port", int(port))
        if api_is_running():
            # Rebound now rather than at the next launch: a user who changes
            # the port with the API on means this one.
            stop_api()
            start_api(int(port))
            self._sync()

    def _copy_token(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.txt_token.text())
            self.lbl_status.setText("Token copied to the clipboard.")

    def _renew(self) -> None:
        confirm = QMessageBox.question(
            self,
            "New token",
            "Issue a new token?\n\nEvery program configured with the current "
            "one stops working until it is given the new one.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        server = self._server()
        if server is None:
            return
        try:
            server.renew_token()
        except Exception as exc:
            logging.exception("Job Manager: could not renew the API token")
            self.lbl_status.setText(f"The token was not replaced: {exc}")
            return
        self._sync()
        self.lbl_status.setText("A new token is in force.")


__all__ = ["ApiDialog"]
