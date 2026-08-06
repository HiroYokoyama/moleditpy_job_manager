"""Host profile editor with a Test Connection button."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .credentials import ensure_password
from .models import BACKEND_OPENSSH, BACKEND_PARAMIKO, HostProfile
from .schedulers import available_schedulers
from .service import JobService
from .tasks import run_async
from .transport import paramiko_available
from .transport.base import HostKeyRejected


class HostsDialog(QDialog):
    """Create, edit and remove host profiles."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.store = service.store
        self.setWindowTitle("Job Manager - Hosts")
        self.resize(720, 560)
        self._current: Optional[HostProfile] = None
        self._build_ui()
        self._reload_list()

    # --- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)

        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.currentItemChanged.connect(lambda *_: self._load_selected())
        left.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._add_host)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_host)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        left.addLayout(buttons)
        outer.addLayout(left, 1)

        right = QVBoxLayout()
        form_box = QGroupBox("Connection")
        form = QFormLayout(form_box)

        self.txt_name = QLineEdit()
        self.txt_hostname = QLineEdit()
        self.txt_username = QLineEdit()
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(22)

        self.cmb_backend = QComboBox()
        self.cmb_backend.addItem("OpenSSH (system ssh, keys/agent)", BACKEND_OPENSSH)
        self.cmb_backend.addItem("paramiko (password supported)", BACKEND_PARAMIKO)
        self.cmb_backend.currentIndexChanged.connect(self._update_backend_hint)

        self.lbl_backend_hint = QLabel("")
        self.lbl_backend_hint.setWordWrap(True)

        self.cmb_scheduler = QComboBox()
        for scheduler in available_schedulers():
            self.cmb_scheduler.addItem(scheduler.label, scheduler.name)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.txt_key = QLineEdit()
        self.txt_key.setPlaceholderText("optional - leave empty to use the agent / ssh_config")
        browse = QPushButton("...")
        browse.setMaximumWidth(32)
        browse.clicked.connect(self._browse_key)
        key_layout.addWidget(self.txt_key)
        key_layout.addWidget(browse)

        self.txt_jump = QLineEdit()
        self.txt_jump.setPlaceholderText("user@bastion (ProxyJump), optional")
        self.txt_remote_root = QLineEdit()

        form.addRow("Display name", self.txt_name)
        form.addRow("Hostname", self.txt_hostname)
        form.addRow("Username", self.txt_username)
        form.addRow("Port", self.spin_port)
        form.addRow("Backend", self.cmb_backend)
        form.addRow("", self.lbl_backend_hint)
        form.addRow("Scheduler", self.cmb_scheduler)
        form.addRow("Private key", key_row)
        form.addRow("Jump host", self.txt_jump)
        form.addRow("Remote root", self.txt_remote_root)
        right.addWidget(form_box)

        adv_box = QGroupBox("Advanced")
        adv = QFormLayout(adv_box)
        self.txt_login = QPlainTextEdit()
        self.txt_login.setPlaceholderText("source /etc/profile\nmodule purge")
        self.txt_login.setMaximumHeight(70)
        self.txt_options = QPlainTextEdit()
        self.txt_options.setPlaceholderText("StrictHostKeyChecking=yes\nServerAliveInterval=30")
        self.txt_options.setMaximumHeight(70)
        self.spin_connect_timeout = QSpinBox()
        self.spin_connect_timeout.setRange(5, 300)
        self.spin_connect_timeout.setSuffix(" s")
        self.spin_command_timeout = QSpinBox()
        self.spin_command_timeout.setRange(10, 3600)
        self.spin_command_timeout.setSuffix(" s")
        adv.addRow("Login commands", self.txt_login)
        adv.addRow("ssh -o options", self.txt_options)
        adv.addRow("Connect timeout", self.spin_connect_timeout)
        adv.addRow("Command timeout", self.spin_command_timeout)
        right.addWidget(adv_box)

        self.chk_ask_password = QCheckBox(
            "Ask for a password when connecting (kept in memory for this session only)"
        )
        right.addWidget(self.chk_ask_password)

        action_row = QHBoxLayout()
        self.btn_test = QPushButton("Test Connection")
        self.btn_test.clicked.connect(self._test_connection)
        self.lbl_test = QLabel("")
        self.lbl_test.setWordWrap(True)
        action_row.addWidget(self.btn_test)
        action_row.addWidget(self.lbl_test, 1)
        right.addLayout(action_row)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        box.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self._save_current)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        right.addWidget(box)

        outer.addLayout(right, 2)
        self._update_backend_hint()

    # --- list handling ------------------------------------------------------

    def _reload_list(self, select_id: str = "") -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for host in self.store.host_list():
            item = QListWidgetItem(f"{host.name}  ({host.target})")
            item.setData(Qt.ItemDataRole.UserRole, host.id)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.list.count():
            target_row = 0
            if select_id:
                for row in range(self.list.count()):
                    if self.list.item(row).data(Qt.ItemDataRole.UserRole) == select_id:
                        target_row = row
                        break
            self.list.setCurrentRow(target_row)
        else:
            self._current = None
            self._clear_form()

    def _selected_host(self) -> Optional[HostProfile]:
        item = self.list.currentItem()
        if item is None:
            return None
        return self.store.hosts.get(item.data(Qt.ItemDataRole.UserRole))

    def _clear_form(self) -> None:
        self.txt_name.setText("")
        self.txt_hostname.setText("")
        self.txt_username.setText("")
        self.spin_port.setValue(22)
        self.txt_key.setText("")
        self.txt_jump.setText("")
        self.txt_remote_root.setText("~/moleditpy_jobs")
        self.txt_login.setPlainText("")
        self.txt_options.setPlainText("")
        self.spin_connect_timeout.setValue(10)
        self.spin_command_timeout.setValue(60)
        self.chk_ask_password.setChecked(False)

    def _load_selected(self) -> None:
        host = self._selected_host()
        self._current = host
        if host is None:
            self._clear_form()
            return
        self.txt_name.setText(host.name)
        self.txt_hostname.setText(host.hostname)
        self.txt_username.setText(host.username)
        self.spin_port.setValue(int(host.port or 22))
        index = self.cmb_backend.findData(host.backend)
        self.cmb_backend.setCurrentIndex(max(0, index))
        index = self.cmb_scheduler.findData(host.scheduler)
        self.cmb_scheduler.setCurrentIndex(max(0, index))
        self.txt_key.setText(host.key_path)
        self.txt_jump.setText(host.jump_host)
        self.txt_remote_root.setText(host.remote_root)
        self.txt_login.setPlainText("\n".join(host.login_commands or []))
        self.txt_options.setPlainText("\n".join(host.ssh_options or []))
        self.spin_connect_timeout.setValue(int(host.connect_timeout or 10))
        self.spin_command_timeout.setValue(int(host.command_timeout or 60))
        self.chk_ask_password.setChecked(bool(host.ask_password))
        self.lbl_test.setText("")

    def _add_host(self) -> None:
        host = HostProfile(name="new host", remote_root="~/moleditpy_jobs")
        self.store.add_host(host)
        self._reload_list(select_id=host.id)

    def _remove_host(self) -> None:
        host = self._selected_host()
        if host is None:
            return
        confirm = QMessageBox.question(
            self,
            "Remove host",
            f"Remove '{host.name}'? Presets for this host are removed too.\n"
            "Jobs already submitted stay in the list but can no longer be polled.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_host(host.id)
        # Do not keep a secret for a host that no longer exists.
        self.service.set_password(host.id, "")
        self._reload_list()

    # --- editing ------------------------------------------------------------

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select private key")
        if path:
            self.txt_key.setText(path)

    def _update_backend_hint(self) -> None:
        backend = self.cmb_backend.currentData()
        if backend == BACKEND_PARAMIKO and not paramiko_available():
            self.lbl_backend_hint.setText(
                "paramiko is not installed - run 'pip install paramiko' to use this backend."
            )
        elif backend == BACKEND_PARAMIKO:
            self.lbl_backend_hint.setText(
                "Passwords are held in memory for this session only and never written to disk."
            )
        else:
            self.lbl_backend_hint.setText(
                "Uses your ~/.ssh/config, agent and keys. Batch mode: password logins are not "
                "possible with this backend."
            )

    def _collect(self, host: HostProfile) -> HostProfile:
        host.name = self.txt_name.text().strip() or "cluster"
        host.hostname = self.txt_hostname.text().strip()
        host.username = self.txt_username.text().strip()
        host.port = int(self.spin_port.value())
        host.backend = self.cmb_backend.currentData()
        host.scheduler = self.cmb_scheduler.currentData()
        host.key_path = self.txt_key.text().strip()
        host.jump_host = self.txt_jump.text().strip()
        host.remote_root = self.txt_remote_root.text().strip() or "~/moleditpy_jobs"
        host.login_commands = [
            line.strip() for line in self.txt_login.toPlainText().splitlines() if line.strip()
        ]
        host.ssh_options = [
            line.strip() for line in self.txt_options.toPlainText().splitlines() if line.strip()
        ]
        host.ask_password = bool(self.chk_ask_password.isChecked())
        host.connect_timeout = int(self.spin_connect_timeout.value())
        host.command_timeout = int(self.spin_command_timeout.value())
        return host

    def _save_current(self) -> Optional[HostProfile]:
        host = self._current or self._selected_host()
        if host is None:
            return None
        self._collect(host)
        self.store.add_host(host)
        self._reload_list(select_id=host.id)
        return host

    # --- connection test ----------------------------------------------------

    def _test_connection(self) -> None:
        host = self._save_current()
        if host is None:
            return
        if not host.hostname:
            self.lbl_test.setText("Enter a hostname first.")
            return
        if not ensure_password(self.service, host, self):
            self.lbl_test.setText("Cancelled.")
            return
        self.btn_test.setEnabled(False)
        self.lbl_test.setText("Connecting...")

        def work() -> str:
            transport = self.service.transport_for(host)
            try:
                return transport.test_connection()
            finally:
                transport.close()

        def ok(remote_name: str) -> None:
            self.btn_test.setEnabled(True)
            self.lbl_test.setText(f"Connected to {remote_name or host.hostname}.")

        def failed(message: str) -> None:
            self.btn_test.setEnabled(True)
            self.lbl_test.setText(message.splitlines()[0] if message else "Connection failed.")
            if "known_hosts" in message.lower() or HostKeyRejected.__name__ in message:
                self._offer_trust(host)

        run_async(self.service.pool, work, on_success=ok, on_error=failed)

    def _offer_trust(self, host: HostProfile) -> None:
        from .transport.paramiko_backend import PARAMIKO_AVAILABLE, trust_host_key

        if not PARAMIKO_AVAILABLE:
            QMessageBox.information(
                self,
                "Unknown host key",
                "This host is not in known_hosts. Connect once with 'ssh "
                f"{host.target}' in a terminal and accept the fingerprint.",
            )
            return
        confirm = QMessageBox.question(
            self,
            "Unknown host key",
            f"{host.hostname} is not in your known_hosts file.\n\n"
            "Only continue if you expect this host to be new. Add its current key?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            fingerprint = trust_host_key(host.hostname, host.port)
        except Exception as exc:
            QMessageBox.warning(self, "Host key", str(exc))
            return
        self.lbl_test.setText(f"Host key added ({fingerprint[:16]}...). Test again.")
