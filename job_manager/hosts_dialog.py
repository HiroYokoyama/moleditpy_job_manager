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
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION
from .credentials import ensure_password, needs_password
from .models import (
    BACKEND_LOCAL,
    BACKEND_OPENSSH,
    BACKEND_PARAMIKO,
    MODE_LANES,
    MODE_RUNNER,
    SCHEDULER_SHELL,
    SCHEDULER_WINDOWS,
    HostProfile,
)
from .runner import apply_queue_limits, probe_resources, queue_paused, set_queue_paused
from .schedulers import available_schedulers
from .service import JobService
from .tasks import run_async
from .transport import local_shell_available, paramiko_available
from .transport.local import INSTALL_HINT as LOCAL_INSTALL_HINT
from .transport.local import POWERSHELL_HINT, SHELL_POSIX, SHELL_POWERSHELL
from .transport.base import HostKeyRejected

#: Shown wherever a password is on offer. Keys are the easier option as well as
#: the safer one, which is the part users tend not to be told: no prompt on
#: every session, and the default OpenSSH backend then works with no extra
#: package at all.
KEY_TIP = (
    "An SSH key is usually less work than a password, not more.\n\n"
    "Once, on this machine:\n"
    "    ssh-keygen -t ed25519\n"
    "    ssh-copy-id user@cluster\n\n"
    "After that the OpenSSH backend connects with no prompt, no paramiko, and "
    "nothing kept in memory. Most clusters expect keys anyway, and many refuse "
    "password logins outright."
)


class HostsDialog(QDialog):
    """Create, edit and remove host profiles."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.store = service.store
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Hosts")
        self.resize(720, 560)
        self._current: Optional[HostProfile] = None
        #: True while the pause box is being set to match the host, so that
        #: showing a state does not ask the host to change to it.
        self._syncing_pause = False
        #: The host whose queue state has already been read, so that a save --
        #: which reloads and re-selects -- does not ask the host again.
        self._queue_state_for = ""
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
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_host)
        buttons.addWidget(add)
        buttons.addWidget(self.btn_remove)
        left.addLayout(buttons)
        outer.addLayout(left, 1)

        # The editing column scrolls: Connection, Advanced and the queue row
        # together are taller than a laptop screen, and Save must stay reachable.
        right_panel = QWidget()
        right = QVBoxLayout(right_panel)
        right.setContentsMargins(0, 0, 0, 0)
        self.form_box = QGroupBox("Connection")
        form = QFormLayout(self.form_box)

        self.txt_name = QLineEdit()
        self.txt_hostname = QLineEdit()
        self.txt_username = QLineEdit()
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(22)

        self.cmb_backend = QComboBox()
        self.cmb_backend.addItem("OpenSSH (system ssh, keys/agent)", BACKEND_OPENSSH)
        self.cmb_backend.addItem("paramiko (password supported)", BACKEND_PARAMIKO)
        self.cmb_backend.addItem("This machine (no SSH)", BACKEND_LOCAL)
        self.cmb_backend.currentIndexChanged.connect(self._update_backend_hint)

        self.lbl_backend_hint = QLabel("")
        self.lbl_backend_hint.setWordWrap(True)

        self.cmb_scheduler = QComboBox()
        for scheduler in available_schedulers():
            self.cmb_scheduler.addItem(scheduler.label, scheduler.name)
        self.cmb_scheduler.currentIndexChanged.connect(self._update_concurrency_row)
        # Which shell the local backend needs depends on the scheduler, so the
        # hint has to follow it as well as the backend.
        self.cmb_scheduler.currentIndexChanged.connect(self._update_backend_hint)

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

        self.spin_max_concurrent = QSpinBox()
        self.spin_max_concurrent.setRange(0, 64)
        self.spin_max_concurrent.setSpecialValueText("no limit")
        self.spin_max_concurrent.setToolTip(
            "Run at most this many jobs at a time on this host.\n\n"
            "Meant for the no-queue mode, where nothing else stops several "
            "submissions piling onto the same cores. A queue already schedules "
            "for you, so leave it at 'no limit' on SLURM, PBS or SGE unless you "
            "have a reason not to.\n\n"
            "Jobs over the limit are chained behind the shortest lane, so the "
            "waiting happens on the host and holds with MoleditPy closed. The "
            "limit applies whether or not you asked for chaining.\n\n"
            "With the helper queue, 'no limit' means the cores below decide: "
            "jobs run together for as long as there are cores for them."
        )

        form.addRow("Display name", self.txt_name)
        form.addRow("Hostname", self.txt_hostname)
        form.addRow("Username", self.txt_username)
        form.addRow("Port", self.spin_port)
        form.addRow("Backend", self.cmb_backend)
        form.addRow("", self.lbl_backend_hint)
        form.addRow("Scheduler", self.cmb_scheduler)
        form.addRow("Private key", key_row)
        form.addRow("Jump host", self.txt_jump)
        self.cmb_concurrency = QComboBox()
        # The helper first, because it is the default and the better of the two:
        # it is the only one that can schedule on cores and memory at all.
        self.cmb_concurrency.addItem("Queue them with a helper on the host", MODE_RUNNER)
        self.cmb_concurrency.addItem("Chain the jobs together", MODE_LANES)
        self.cmb_concurrency.setToolTip(
            "How the limit above is kept.\n\n"
            "Chaining leaves nothing behind on the host: each job is told to "
            "wait for another, and the order is fixed when you submit.\n\n"
            "The helper is a small script that holds a real queue on the host. "
            "It can count cores rather than jobs, free a slot the moment "
            "something ends, cancel a job that has not started, and reorder "
            "what is waiting -- and it exits by itself as soon as the queue is "
            "empty. It needs a POSIX shell, so it is offered only where there "
            "is no scheduler already doing the job."
        )
        self.cmb_concurrency.currentIndexChanged.connect(self._update_concurrency_row)

        self.chk_detect_resources = QCheckBox("Ask the host instead")
        self.chk_detect_resources.setToolTip(
            "Let the helper read the machine's own core count and memory "
            "(nproc, /proc/meminfo) instead of the two numbers below.\n\n"
            "Off by default, because what the machine reports is the whole "
            "machine: on anything shared, that is not the share you are "
            "entitled to. The number you know beats the number it reports.\n\n"
            "The Detect button fills the fields in without handing the budget "
            "over, which is usually what you want: see what it has, then decide."
        )
        self.chk_detect_resources.toggled.connect(self._on_detect_toggled)

        self.spin_runner_cores = QSpinBox()
        self.spin_runner_cores.setRange(1, 4096)
        self.spin_runner_cores.setToolTip(
            "How many cores the helper may hand out. Each job asks for as many "
            "as its preset's 'CPUs per task', and starts when that many are "
            "free."
        )

        self.spin_runner_memory = QSpinBox()
        self.spin_runner_memory.setRange(1, 8192)
        self.spin_runner_memory.setSuffix(" GB")
        self.spin_runner_memory.setToolTip(
            "How much memory the helper may hand out, in total.\n\n"
            "A second budget beside the cores, and the one that matters most: "
            "two jobs asking for 90 GB each must not both start on a 120 GB "
            "machine merely because the cores were free. Overcommitting memory "
            "does not slow a calculation down, it gets it killed hours in.\n\n"
            "Each job asks for its preset's Memory field, which the wizard "
            "fills in from the input file where it can. A job that asks for "
            "nothing waits for nothing."
        )

        form.addRow("Remote root", self.txt_remote_root)
        form.addRow("Run at most", self.spin_max_concurrent)
        form.addRow("Queueing", self.cmb_concurrency)
        form.addRow("Cores available", self.spin_runner_cores)
        form.addRow("Memory available", self.spin_runner_memory)
        form.addRow("", self.chk_detect_resources)
        right.addWidget(self.form_box)

        self.adv_box = QGroupBox("Advanced")
        adv = QFormLayout(self.adv_box)
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
        self.chk_load_profile = QCheckBox("Read the login files first")
        self.chk_load_profile.setToolTip(
            "Run /etc/profile, ~/.bash_profile, ~/.profile and ~/.bashrc before "
            "anything else -- for every command sent to the host, and at the top "
            "of every job script.\n\n"
            "'ssh host command' gets a shell that is neither login nor "
            "interactive, so none of those files is read, while logging in by "
            "hand reads all of them. That is why a program you can run over SSH "
            "yourself is 'command not found' in the job.\n\n"
            "Each file is optional and allowed to fail, so a host missing one "
            "is not an error. Note that Debian's stock ~/.bashrc stops early "
            "for a non-interactive shell: keep module loads above that guard, "
            "or name them in Login commands below."
        )
        adv.addRow("Environment", self.chk_load_profile)
        adv.addRow("Login commands", self.txt_login)
        adv.addRow("ssh -o options", self.txt_options)
        adv.addRow("Connect timeout", self.spin_connect_timeout)
        adv.addRow("Command timeout", self.spin_command_timeout)
        right.addWidget(self.adv_box)

        self.chk_ask_password = QCheckBox(
            "Ask for a password when connecting (kept in memory for this session only)"
        )
        self.chk_ask_password.setToolTip(KEY_TIP)
        right.addWidget(self.chk_ask_password)

        self.lbl_key_tip = QLabel(
            "Tip: a key is less work than a password — set one up once with "
            "<code>ssh-keygen -t ed25519</code> then "
            "<code>ssh-copy-id user@host</code>, and this host connects without "
            "asking again."
        )
        self.lbl_key_tip.setWordWrap(True)
        self.lbl_key_tip.setStyleSheet("color: palette(mid);")
        self.lbl_key_tip.setToolTip(KEY_TIP)
        right.addWidget(self.lbl_key_tip)

        self.queue_box = QGroupBox("Queue on the host")
        queue_layout = QHBoxLayout(self.queue_box)
        self.chk_pause = QCheckBox("Hold the queue")
        self.chk_pause.setToolTip(
            "Stop the helper starting anything new. Jobs already running are "
            "left alone -- a pause that killed them would mean throwing away "
            "however long they have been going.\n\n"
            "The flag lives on the host, so it outlasts this dialog, this "
            "session, and the helper's own comings and goings."
        )
        self.chk_pause.toggled.connect(self._on_pause_toggled)
        self.btn_apply_limits = QPushButton("Apply limits now")
        self.btn_apply_limits.setToolTip(
            "Send 'Run at most' and 'Cores available' to a helper that is "
            "already running.\n\n"
            "Submitting a job sends them too, so this is for changing your "
            "mind while jobs are queued -- which is exactly when waiting for "
            "the next submission is no use."
        )
        self.btn_apply_limits.clicked.connect(self._apply_queue_limits)
        self.btn_detect = QPushButton("Detect")
        self.btn_detect.setToolTip(
            "Ask the host how many cores and how much memory it has, and put "
            "the answers in the two fields above.\n\n"
            "Left at 'detect' the helper asks for itself, so this is for when "
            "you want to see the numbers -- or to give the queue a smaller "
            "share of a machine you do not have to yourself."
        )
        self.btn_detect.clicked.connect(self._detect_resources)
        self.lbl_queue = QLabel("")
        self.lbl_queue.setWordWrap(True)
        queue_layout.addWidget(self.chk_pause)
        queue_layout.addWidget(self.btn_detect)
        queue_layout.addWidget(self.btn_apply_limits)
        queue_layout.addWidget(self.lbl_queue, 1)
        right.addWidget(self.queue_box)

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
        self.btn_save = box.button(QDialogButtonBox.StandardButton.Save)
        self.btn_save.clicked.connect(self._save_current)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)

        column = QVBoxLayout()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(right_panel)
        column.addWidget(scroll, 1)
        # Outside the scroll area: Save and Close stay put however far it scrolls.
        column.addWidget(box)
        outer.addLayout(column, 2)
        # After every widget exists: this one now also shows or hides the queue
        # controls, which are built further down than the rows that drive it.
        self._update_concurrency_row()
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

    def _set_editor_enabled(self, enabled: bool) -> None:
        """Nothing is editable until a host is selected.

        Everything the form collects belongs to the selected profile, so with
        no selection there is nowhere for a keystroke to go: Save and Test
        Connection both found no host and returned in silence, which reads as
        the buttons being broken rather than as nothing being selected.
        """
        for widget in (
            self.form_box,
            self.adv_box,
            self.chk_ask_password,
            self.queue_box,
            self.btn_test,
            self.btn_save,
            self.btn_remove,
        ):
            widget.setEnabled(enabled)

    def _clear_form(self) -> None:
        self.txt_name.setText("")
        self.txt_hostname.setText("")
        self.txt_username.setText("")
        self.spin_port.setValue(22)
        self.txt_key.setText("")
        self.txt_jump.setText("")
        self.txt_remote_root.setText("~/moleditpy_jobs")
        self.spin_max_concurrent.setValue(0)
        self.cmb_concurrency.setCurrentIndex(max(0, self.cmb_concurrency.findData(MODE_RUNNER)))
        self.spin_runner_cores.setValue(self.spin_runner_cores.minimum())
        self.spin_runner_memory.setValue(self.spin_runner_memory.minimum())
        self.chk_detect_resources.setChecked(False)
        self.chk_load_profile.setChecked(True)
        self.txt_login.setPlainText("")
        self.txt_options.setPlainText("")
        self.spin_connect_timeout.setValue(10)
        self.spin_command_timeout.setValue(60)
        self.chk_ask_password.setChecked(False)
        self._set_pause_checkbox(False)
        self.lbl_queue.setText("")
        self._set_editor_enabled(False)
        self.lbl_test.setText("No host selected - press Add to create one.")

    def _load_selected(self) -> None:
        host = self._selected_host()
        self._current = host
        if host is None:
            self._clear_form()
            return
        self._set_editor_enabled(True)
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
        self.spin_max_concurrent.setValue(max(0, int(host.max_concurrent or 0)))
        index = self.cmb_concurrency.findData(host.concurrency_mode or MODE_LANES)
        self.cmb_concurrency.setCurrentIndex(max(0, index))
        self.chk_detect_resources.setChecked(bool(host.runner_detect))
        # A detecting host stores 0 for both, so the boxes show their minimum
        # rather than a budget it never had.
        self.spin_runner_cores.setValue(max(1, int(host.runner_cores or 0)))
        # Stored in MB, shown in GB: nobody sizes a machine in megabytes.
        self.spin_runner_memory.setValue(max(1, int(host.runner_memory_mb or 0) // 1024))
        self.chk_load_profile.setChecked(bool(host.load_profile))
        self._update_concurrency_row()
        self.txt_login.setPlainText("\n".join(host.login_commands or []))
        self.txt_options.setPlainText("\n".join(host.ssh_options or []))
        self.spin_connect_timeout.setValue(int(host.connect_timeout or 10))
        self.spin_command_timeout.setValue(int(host.command_timeout or 60))
        self.chk_ask_password.setChecked(bool(host.ask_password))
        # Explicitly, not only from the combo's signal: selecting a host whose
        # backend matches the one already shown changes no index, and the box
        # would keep the state the previous host left it in.
        self._update_backend_hint()
        self.lbl_test.setText("")
        self._refresh_queue_state()

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

    def _update_concurrency_row(self) -> None:
        """The helper is only offered where nothing else is scheduling."""
        # Both no-queue schedulers have a runner; a real cluster does not need
        # one and should not be offered it.
        shell = self.cmb_scheduler.currentData() in (SCHEDULER_SHELL, SCHEDULER_WINDOWS)
        self.cmb_concurrency.setEnabled(shell)
        if not shell and self.cmb_concurrency.currentData() == MODE_RUNNER:
            self.cmb_concurrency.setCurrentIndex(self.cmb_concurrency.findData(MODE_LANES))
        runner = shell and self.cmb_concurrency.currentData() == MODE_RUNNER
        detect = self.chk_detect_resources.isChecked()
        self.chk_detect_resources.setEnabled(runner)
        # Grey rather than hidden while detecting: the numbers are still worth
        # seeing, and pressing Detect fills them in without handing over.
        self.spin_runner_cores.setEnabled(runner and not detect)
        self.spin_runner_memory.setEnabled(runner and not detect)
        # Nothing to hold or to send limits to unless there is a helper.
        self.queue_box.setVisible(runner)

    def _on_detect_toggled(self, checked: bool) -> None:
        """Detection is opt-in, so it only greys the fields it takes over."""
        self._update_concurrency_row()
        if checked:
            self.lbl_queue.setText("The helper will read the machine's own cores and memory.")

    def _update_backend_hint(self) -> None:
        backend = self.cmb_backend.currentData()
        self._set_ssh_fields_enabled(backend != BACKEND_LOCAL)
        # Only where a password is actually on offer; the other backends never
        # ask for one, so the advice would be noise.
        self.lbl_key_tip.setVisible(backend == BACKEND_PARAMIKO)
        # And the box itself is live only there: OpenSSH runs in batch mode and
        # cannot do password authentication at all, so ticking it there looked
        # like a choice and did nothing.
        self.chk_ask_password.setEnabled(backend == BACKEND_PARAMIKO)
        if backend == BACKEND_LOCAL:
            # Which shell has to be there follows the scheduler: a Windows host
            # is driven entirely through PowerShell and needs no bash at all.
            kind = (
                SHELL_POWERSHELL
                if self.cmb_scheduler.currentData() == SCHEDULER_WINDOWS
                else SHELL_POSIX
            )
            if local_shell_available(kind):
                self.lbl_backend_hint.setText(
                    "Runs the job here, with no network at all. Remote root is a "
                    "directory on this machine; hostname and keys are not used."
                )
            elif kind == SHELL_POWERSHELL:
                self.lbl_backend_hint.setText(POWERSHELL_HINT)
            else:
                self.lbl_backend_hint.setText(LOCAL_INSTALL_HINT)
            return
        if self.cmb_scheduler.currentData() == SCHEDULER_WINDOWS:
            # Every command sent to this host is PowerShell, which only works
            # over SSH if the remote sshd's default shell is PowerShell too --
            # not the default on Windows, and not something this end can check.
            self.lbl_backend_hint.setText(
                "The Windows scheduler sends PowerShell. Over SSH that needs the "
                "remote machine's default SSH shell to be PowerShell; the tested "
                "combination is 'This machine (no SSH)'."
            )
            return
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

    def _set_ssh_fields_enabled(self, enabled: bool) -> None:
        """Nothing about the network applies when the host is this machine."""
        for widget in (
            self.txt_hostname,
            self.txt_username,
            self.spin_port,
            self.txt_key,
            self.txt_jump,
            self.spin_connect_timeout,
        ):
            widget.setEnabled(enabled)

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
        host.max_concurrent = int(self.spin_max_concurrent.value())
        host.concurrency_mode = self.cmb_concurrency.currentData() or MODE_LANES
        host.runner_detect = bool(self.chk_detect_resources.isChecked())
        # 0 is what tells the helper to read the machine itself, so a detecting
        # host stores nothing rather than a number the user never chose.
        host.runner_cores = 0 if host.runner_detect else int(self.spin_runner_cores.value())
        host.runner_memory_mb = (
            0 if host.runner_detect else int(self.spin_runner_memory.value()) * 1024
        )
        host.load_profile = bool(self.chk_load_profile.isChecked())
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
        # After the reload, which re-selects and so clears this label. Saving
        # was silent before, which is indistinguishable from a Save that did
        # nothing at all.
        self.lbl_test.setText(f"Saved '{host.name}'.")
        return host

    def _persist_current(self) -> Optional[HostProfile]:
        """Apply the form to the selected profile without rebuilding the list.

        ``_save_current`` reloads the list, and reloading re-selects, which
        reloads the form: harmless for a one-shot connection test, but it would
        fight a control whose state is being read back from the host.
        """
        host = self._current or self._selected_host()
        if host is None:
            return None
        self._collect(host)
        self.store.save_settings()
        return host

    # --- the queue on the host ----------------------------------------------

    def _set_pause_checkbox(self, paused: bool) -> None:
        """Show a state without asking the host to change to it."""
        self._syncing_pause = True
        try:
            self.chk_pause.setChecked(bool(paused))
        finally:
            self._syncing_pause = False

    def _refresh_queue_state(self) -> None:
        """Read whether the selected host's queue is held.

        One small command, and only when a host that has a queue is selected in
        a dialog the user opened deliberately. A host that would pop a password
        prompt is left alone: a dialog appearing because you clicked a name in
        a list is not something anyone asked for.
        """
        host = self._current
        if host is not None and host.id == self._queue_state_for:
            # Already asked for this host. Saving the profile reloads the list,
            # which re-selects, which lands here -- so without this, pressing
            # Save or Test Connection put another command on the wire.
            return
        self._set_pause_checkbox(False)
        self._queue_state_for = ""
        if host is None or not host.uses_remote_runner:
            self.lbl_queue.setText("")
            return
        self._queue_state_for = host.id
        if needs_password(self.service, host):
            self.chk_pause.setEnabled(False)
            self.lbl_queue.setText("Test the connection first to read the queue.")
            return
        self.chk_pause.setEnabled(False)
        self.lbl_queue.setText("Reading the queue...")
        host_id = host.id

        def work() -> bool:
            transport = self.service.transport_for(host)
            try:
                return queue_paused(transport, host)
            finally:
                transport.close()

        def ok(paused: bool) -> None:
            # The selection may have moved on while the answer was in flight,
            # and it would be describing a different host by the time it lands.
            if self._current is None or self._current.id != host_id:
                return
            self.chk_pause.setEnabled(True)
            self._set_pause_checkbox(paused)
            self.lbl_queue.setText("The queue is held." if paused else "The queue is running.")

        def failed(message: str) -> None:
            if self._current is None or self._current.id != host_id:
                return
            self.chk_pause.setEnabled(True)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not read the queue."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed)

    def _on_pause_toggled(self, checked: bool) -> None:
        if self._syncing_pause:
            return
        host = self._persist_current()
        if host is None or not host.uses_remote_runner:
            return
        if not ensure_password(self.service, host, self):
            self._set_pause_checkbox(not checked)
            return
        self.chk_pause.setEnabled(False)
        self.lbl_queue.setText("Holding the queue..." if checked else "Letting the queue run...")

        def work() -> bool:
            transport = self.service.transport_for(host)
            try:
                return set_queue_paused(transport, host, checked)
            finally:
                transport.close()

        def ok(paused: bool) -> None:
            self.chk_pause.setEnabled(True)
            self.lbl_queue.setText(
                "The queue is held. Jobs already running continue."
                if paused
                else "The queue is running."
            )

        def failed(message: str) -> None:
            self.chk_pause.setEnabled(True)
            # Back to what the host still says, rather than leaving the box
            # claiming a state the host never took.
            self._set_pause_checkbox(not checked)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not change the queue."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed)

    def _detect_resources(self) -> None:
        """Fill the two budgets from what the host actually has."""
        host = self._persist_current()
        if host is None:
            return
        if not ensure_password(self.service, host, self):
            return
        self.btn_detect.setEnabled(False)
        self.lbl_queue.setText("Asking the host...")

        def work() -> tuple:
            transport = self.service.transport_for(host)
            try:
                return probe_resources(transport, host)
            finally:
                transport.close()

        def ok(found: tuple) -> None:
            self.btn_detect.setEnabled(True)
            cores, memory_mb, threads = found
            if not cores and not memory_mb:
                self.lbl_queue.setText("The host did not say what it has.")
                return
            if cores or memory_mb:
                # Filling the fields is the opposite of handing the budget over:
                # the point of the button is to see the numbers and then decide.
                self.chk_detect_resources.setChecked(False)
            if cores:
                self.spin_runner_cores.setValue(min(cores, self.spin_runner_cores.maximum()))
            if memory_mb:
                # Rounded down to whole gigabytes, which is the unit the field
                # is in: offering a budget larger than the machine would defeat
                # the point of asking.
                self.spin_runner_memory.setValue(
                    min(memory_mb // 1024, self.spin_runner_memory.maximum())
                )
            # Threads are named separately on purpose: a user who knows the
            # machine as "12 cores" should see why the budget says 8, rather
            # than assume the detection is broken.
            threads_note = (
                f" ({threads} hardware threads, but a calculation gains nothing"
                " from running on more than one thread per core)"
                if threads > cores > 0
                else ""
            )
            self.lbl_queue.setText(
                f"{host.name} reports {cores or '?'} cores and "
                f"{(memory_mb // 1024) if memory_mb else '?'} GB{threads_note}. "
                "Lower them to leave room for other users."
            )

        def failed(message: str) -> None:
            self.btn_detect.setEnabled(True)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not ask the host."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed)

    def _apply_queue_limits(self) -> None:
        host = self._persist_current()
        if host is None or not host.uses_remote_runner:
            return
        if not ensure_password(self.service, host, self):
            return
        self.btn_apply_limits.setEnabled(False)
        self.lbl_queue.setText("Sending the limits...")

        def work() -> None:
            transport = self.service.transport_for(host)
            try:
                apply_queue_limits(transport, host)
            finally:
                transport.close()

        def ok(_result) -> None:
            self.btn_apply_limits.setEnabled(True)
            cores = host.runner_cores or 0
            self.lbl_queue.setText(
                f"The helper will run at most {max(1, host.max_concurrent or 1)} job(s), "
                + (f"using up to {cores} core(s)." if cores else "using every core it finds.")
            )

        def failed(message: str) -> None:
            self.btn_apply_limits.setEnabled(True)
            self.lbl_queue.setText(
                message.splitlines()[0] if message else "Could not send the limits."
            )

        run_async(self.service.pool, work, on_success=ok, on_error=failed)

    # --- connection test ----------------------------------------------------

    def _test_connection(self) -> None:
        host = self._save_current()
        if host is None:
            self.lbl_test.setText("Select a host first.")
            return
        if not host.hostname and not host.is_local:
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
