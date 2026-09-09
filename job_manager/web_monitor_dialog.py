"""Start or stop the read-only web view, and hand over the two things needed
to reach it from somewhere else: the URL, and the ``tailscale serve`` command
that publishes it to the tailnet.

Running it is one press, but never on the GUI thread: the first serve waits on
a certificate, and blocking here froze the whole application for as long as it
took. Publishing stays an explicit press either way -- putting a machine on a
network is the user's decision, not something to do on their behalf.
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
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION
from .tasks import run_async
from .web_monitor import (
    serve_on_tailnet,
    stop_serving_on_tailnet,
    tailscale_available,
    tailscale_command,
    tailscale_dns_name,
)


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
        self._layout = layout

    def add_button(self, button: QPushButton) -> None:
        """Put another action on this row, to the right of Copy."""
        self._layout.addWidget(button)

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
        #: Whether *this dialog* ran the publish. Tailscale can already be
        #: serving something else, which is not ours to withdraw.
        self._served = False
        self._dns = None
        #: A Tailscale call is in flight; the buttons stay off until it lands.
        self._busy = False
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
        # Run it here rather than only offering it to be pasted: the command is
        # fixed and the only variable in it is the port this dialog just bound,
        # so retyping it in a terminal is a step that can only go wrong.
        self.btn_serve = QPushButton("Run")
        self.btn_serve.setToolTip("Run this command now, and publish the page to your tailnet.")
        self.btn_serve.clicked.connect(self._serve_on_tailnet)
        self.row_cmd.add_button(self.btn_serve)
        self.btn_unserve = QPushButton("Unpublish")
        self.btn_unserve.setToolTip(
            "tailscale serve reset -- withdraw whatever this machine is serving."
        )
        self.btn_unserve.clicked.connect(self._stop_tailnet)
        self.row_cmd.add_button(self.btn_unserve)
        layout.addWidget(self.row_cmd)

        layout.addWidget(QLabel("From your phone or another device on the tailnet:"))
        # A field with a Copy button, not a label: this is the one string that
        # has to reach another device, and a label cannot be copied from a
        # dialog whose text is not selectable.
        self.row_tailnet = _CopyRow()
        layout.addWidget(self.row_tailnet)

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

    def _run_off_thread(self, work, done) -> None:
        """Run a Tailscale call on the pool and answer back on the GUI thread.

        Never inline: the first serve waits on a certificate, and even the
        quick failures are a subprocess. Doing that here froze the whole
        application -- window unredrawable, no cancel -- for as long as it took,
        which is what "the Run button hangs" was.
        """
        self._busy = True
        self._refresh()

        def finished(result) -> None:
            self._busy = False
            done(*result)

        def failed(message: str) -> None:
            self._busy = False
            self._refresh()
            QMessageBox.warning(self, "Tailscale", message)

        run_async(self.monitor.service.pool, work, on_success=finished, on_error=failed, quiet=True)

    def _serve_on_tailnet(self) -> None:
        """One click: run the command shown, then say what happened."""
        if not self._running() or self._busy:
            return
        port = self._server().port

        def done(ok: bool, message: str) -> None:
            self._served = bool(ok)
            self._refresh()
            if ok:
                QMessageBox.information(
                    self,
                    "Published",
                    "The monitor is now on your tailnet.\n\n"
                    "Open the link below on any device signed in to the same "
                    "tailnet. Use Unpublish to withdraw it.",
                )
            else:
                QMessageBox.warning(self, "Tailscale", message)

        self._run_off_thread(lambda: serve_on_tailnet(port), done)

    def _stop_tailnet(self) -> None:
        if self._busy:
            return

        def done(ok: bool, message: str) -> None:
            self._served = False
            self._refresh()
            if not ok:
                QMessageBox.warning(self, "Tailscale", message)

        self._run_off_thread(stop_serving_on_tailnet, done)

    def _refresh(self) -> None:
        server = self._server()
        running = self._running()
        available = tailscale_available()
        if running:
            port = server.port
            self.lbl_state.setText(f"Serving on 127.0.0.1:{port}, read-only.")
            self.row_url.set_text(server.url())
            self.row_cmd.set_text(tailscale_command(port))
            # The real name where Tailscale will tell us, the placeholder only
            # where it will not: a link that has to be hand-edited on a phone
            # before it works is not much of a link.
            self.row_tailnet.set_text(server.tailscale_url(self._dns_name()))
            self.lbl_tailnet.setText(
                "The token is in the link, and is kept as a cookie after the "
                "first load, so a reload does not need it again."
            )
            self.btn_toggle.setText("Stop serving")
        else:
            self.lbl_state.setText("Not serving. Nothing is listening.")
            for row in (self.row_url, self.row_cmd, self.row_tailnet):
                row.set_text("")
            self.lbl_tailnet.setText("")
            self.btn_toggle.setText("Start serving")
        for row in (self.row_url, self.row_cmd, self.row_tailnet):
            row.setEnabled(running)
            row.button.setText("Copy")
        self.btn_serve.setEnabled(running and available and not self._busy)
        self.btn_unserve.setEnabled(running and available and self._served and not self._busy)
        self.btn_serve.setText("Working..." if self._busy else "Run")
        if not available:
            self.lbl_tailnet.setText(
                (self.lbl_tailnet.text() + "\n\n" if running else "")
                + "Tailscale was not found on PATH, so Run is unavailable. The "
                "command above still applies once it is installed; nothing here "
                "needs it to serve on this machine."
            )

    def _dns_name(self) -> str:
        """Asked once per dialog: it shells out, and the answer does not move
        while a window is open."""
        if self._dns is None:
            self._dns = tailscale_dns_name() if tailscale_available() else ""
        return self._dns


__all__ = ["WebMonitorDialog"]
