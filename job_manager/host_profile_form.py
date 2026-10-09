"""Host connection and resource fields, separate from persistence and network work."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .models import (
    BACKEND_LOCAL,
    BACKEND_OPENSSH,
    BACKEND_PARAMIKO,
    BACKEND_WSL,
    MODE_LANES,
    MODE_RUNNER,
    HostProfile,
)
from .schedulers import available_schedulers
from .store import MAX_POLL_INTERVAL, MIN_POLL_INTERVAL

EQUAL_PATH_TIP = (
    "Set this when the host's filesystem is also reachable from this "
    "machine directly -- a Samba/CIFS share, a mapped drive, an sshfs "
    "or NFS mount -- rooted at the same place as 'Remote root' above.\n\n"
    "With it set, Open Result reads a job's files straight from here "
    "instead of downloading them first: the remote path and this local "
    "one are treated as the same files, just reached two different "
    "ways. Leave it empty if there is no such mirror."
)

KEY_TIP = (
    "An SSH key is usually less work than a password, not more.\n\n"
    "Once, on this machine:\n"
    "    ssh-keygen -t ed25519\n"
    "    ssh-copy-id user@cluster\n\n"
    "After that the OpenSSH backend connects with no prompt, no paramiko, and "
    "nothing kept in memory. Most clusters expect keys anyway, and many refuse "
    "password logins outright."
)


class HostProfileForm(QWidget):
    def __init__(self, callbacks, parent=None):
        super().__init__(parent)
        self._apply_queue_limits = callbacks["_apply_queue_limits"]
        self._detect_resources = callbacks["_detect_resources"]
        self._on_detect_toggled = callbacks["_on_detect_toggled"]
        self._on_pause_toggled = callbacks["_on_pause_toggled"]
        self._suggest_local_scheduler = callbacks["_suggest_local_scheduler"]
        self._update_backend_hint = callbacks["_update_backend_hint"]
        self._update_concurrency_row = callbacks["_update_concurrency_row"]
        right = QVBoxLayout(self)
        right.setContentsMargins(0, 0, 0, 0)
        self.form_box = QGroupBox("Connection")
        form = QFormLayout(self.form_box)

        self.chk_enabled = QCheckBox("Enabled")
        self.chk_enabled.setChecked(True)
        self.chk_enabled.setToolTip("Off keeps the profile but skips this host everywhere.")

        self.txt_name = QLineEdit()
        self.txt_hostname = QLineEdit()
        self.txt_username = QLineEdit()
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        self.spin_port.setValue(22)

        self.cmb_backend = QComboBox()
        self.cmb_backend.addItem("OpenSSH (system ssh, keys/agent)", BACKEND_OPENSSH)
        self.cmb_backend.addItem(
            "paramiko (keeps one session; private key, agent or password)", BACKEND_PARAMIKO
        )
        self.cmb_backend.addItem("This machine (no SSH)", BACKEND_LOCAL)
        self.cmb_backend.addItem("This machine, inside WSL (no SSH)", BACKEND_WSL)
        self.cmb_backend.currentIndexChanged.connect(self._update_backend_hint)
        self.cmb_backend.currentIndexChanged.connect(self._suggest_local_scheduler)

        # Editable: a distribution installed after this dialog opened can still
        # be typed in, and the list is only a convenience.
        self.cmb_distro = QComboBox()
        self.cmb_distro.setEditable(True)
        self.cmb_distro.setToolTip("Which WSL distribution to run in. Empty means the default one.")

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

        equal_path_row = QWidget()
        equal_path_layout = QHBoxLayout(equal_path_row)
        equal_path_layout.setContentsMargins(0, 0, 0, 0)
        self.txt_equal_path = QLineEdit()
        self.txt_equal_path.setPlaceholderText(
            "optional - e.g. \\\\server\\share or /mnt/cluster, mirroring Remote root"
        )
        self.txt_equal_path.setToolTip(EQUAL_PATH_TIP)
        equal_path_browse = QPushButton("...")
        equal_path_browse.setMaximumWidth(32)
        equal_path_browse.setToolTip(EQUAL_PATH_TIP)
        equal_path_browse.clicked.connect(self._browse_equal_path)
        equal_path_layout.addWidget(self.txt_equal_path)
        equal_path_layout.addWidget(equal_path_browse)

        self.spin_max_concurrent = QSpinBox()
        self.spin_max_concurrent.setRange(0, 64)
        self.spin_max_concurrent.setSpecialValueText("no limit")
        self.spin_max_concurrent.setToolTip(
            "Run at most this many jobs here at once. 0 means no limit."
        )

        form.addRow("", self.chk_enabled)
        form.addRow("Display name", self.txt_name)
        form.addRow("Hostname", self.txt_hostname)
        form.addRow("Username", self.txt_username)
        form.addRow("Port", self.spin_port)
        form.addRow("Backend", self.cmb_backend)
        form.addRow("", self.lbl_backend_hint)
        form.addRow("Scheduler", self.cmb_scheduler)
        self.row_distro_label = QLabel("WSL distribution")
        form.addRow(self.row_distro_label, self.cmb_distro)
        form.addRow("Private key", key_row)
        form.addRow("Jump host", self.txt_jump)
        self.cmb_concurrency = QComboBox()
        # The helper first, because it is the default and the better of the two:
        # it is the only one that can schedule on cores and memory at all.
        self.cmb_concurrency.addItem("Queue them with a helper on the host", MODE_RUNNER)
        self.cmb_concurrency.addItem("Chain the jobs together", MODE_LANES)
        self.cmb_concurrency.setToolTip(
            "How that limit is kept: a small queue on the host, or jobs chained together."
        )
        self.cmb_concurrency.currentIndexChanged.connect(self._update_concurrency_row)

        self.chk_detect_resources = QCheckBox("Ask the host instead")
        self.chk_detect_resources.setToolTip(
            "Let the queue read the machine's own cores and memory instead of the fields above."
        )
        self.chk_detect_resources.toggled.connect(self._on_detect_toggled)

        self.spin_runner_cores = QSpinBox()
        self.spin_runner_cores.setRange(1, 4096)
        self.spin_runner_cores.setToolTip(
            "Cores the queue may hand out; a job starts when its CPUs per task are free."
        )

        self.spin_runner_memory = QSpinBox()
        self.spin_runner_memory.setRange(1, 8192)
        self.spin_runner_memory.setSuffix(" GB")
        self.spin_runner_memory.setToolTip(
            "Memory the queue may hand out in total, so two large jobs never share too little."
        )

        form.addRow("Remote root", self.txt_remote_root)
        form.addRow("Equal path (local mirror)", equal_path_row)
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
            "Read /etc/profile and the ~/.bash files before every command and in the job script."
        )
        adv.addRow("Environment", self.chk_load_profile)
        adv.addRow("Login commands", self.txt_login)
        adv.addRow("ssh -o options", self.txt_options)
        adv.addRow("Connect timeout", self.spin_connect_timeout)
        adv.addRow("Command timeout", self.spin_command_timeout)
        self.txt_submit_options = QLineEdit()
        self.txt_submit_options.setPlaceholderText("e.g. -W group_list=mygroup")
        self.txt_submit_options.setToolTip(
            "Added to every sbatch / qsub on this host, before the script:\n"
            "qsub <these> moleditpy_run.sh. Written as you would type them; a\n"
            "preset's own submit options come after. Ignored without a queue."
        )
        adv.addRow("Submit options", self.txt_submit_options)
        right.addWidget(self.adv_box)

        self.monitor_box = QGroupBox("Monitoring")
        mon = QFormLayout(self.monitor_box)
        self.chk_monitor_usage = QCheckBox("Sample load and memory in the Host Monitor")
        self.chk_monitor_usage.setToolTip(
            "Untick for a shared login node, such as a supercomputer's: its load is\n"
            "everyone's, and a probe every few seconds is traffic its admins do not\n"
            "want. The Host Monitor still lists this host's jobs."
        )
        self.spin_monitor_interval = QSpinBox()
        self.spin_monitor_interval.setRange(0, 3600)
        self.spin_monitor_interval.setSuffix(" s")
        self.spin_monitor_interval.setSpecialValueText("window setting")
        self.spin_monitor_interval.setToolTip(
            "How often the Host Monitor samples this host. 0 follows the window's own setting."
        )
        self.chk_monitor_usage.toggled.connect(self.spin_monitor_interval.setEnabled)
        self.spin_poll_interval = QSpinBox()
        self.spin_poll_interval.setRange(0, MAX_POLL_INTERVAL)
        self.spin_poll_interval.setSingleStep(30)
        self.spin_poll_interval.setSuffix(" s")
        self.spin_poll_interval.setSpecialValueText("global setting")
        self.spin_poll_interval.setToolTip(
            "How often this host's queue is asked about its jobs. 0 follows the\n"
            f"interval in the Jobs window; anything set is at least {MIN_POLL_INTERVAL} s.\n"
            "Set it higher for a busy login node, lower for your own workstation."
        )
        mon.addRow("", self.chk_monitor_usage)
        mon.addRow("Monitor every", self.spin_monitor_interval)
        mon.addRow("Poll jobs every", self.spin_poll_interval)
        right.addWidget(self.monitor_box)

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
            "Stop the queue starting anything new. Jobs already running are left alone."
        )
        self.chk_pause.toggled.connect(self._on_pause_toggled)
        self.btn_apply_limits = QPushButton("Apply limits now")
        self.btn_apply_limits.setToolTip(
            "Send the limits above to a queue that is already running."
        )
        self.btn_apply_limits.clicked.connect(self._apply_queue_limits)
        self.btn_detect = QPushButton("Detect")
        self.btn_detect.setToolTip("Ask the host what it has, and fill the two fields in.")
        self.btn_detect.clicked.connect(self._detect_resources)
        self.lbl_queue = QLabel("")
        self.lbl_queue.setWordWrap(True)
        queue_layout.addWidget(self.chk_pause)
        queue_layout.addWidget(self.btn_detect)
        queue_layout.addWidget(self.btn_apply_limits)
        queue_layout.addWidget(self.lbl_queue, 1)
        right.addWidget(self.queue_box)

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select private key")
        if path:
            self.txt_key.setText(path)

    def _browse_equal_path(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select the local mirror of this host")
        if path:
            self.txt_equal_path.setText(path)

    def _collect(self, host: HostProfile) -> HostProfile:
        host.enabled = bool(self.chk_enabled.isChecked())
        host.name = self.txt_name.text().strip() or "cluster"
        host.hostname = self.txt_hostname.text().strip()
        host.username = self.txt_username.text().strip()
        host.port = int(self.spin_port.value())
        host.backend = self.cmb_backend.currentData()
        host.scheduler = self.cmb_scheduler.currentData()
        host.key_path = self.txt_key.text().strip()
        host.wsl_distro = self.cmb_distro.currentText().strip()
        host.jump_host = self.txt_jump.text().strip()
        host.remote_root = self.txt_remote_root.text().strip() or "~/moleditpy_jobs"
        host.equal_path = self.txt_equal_path.text().strip()
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
        host.submit_options = self.txt_submit_options.text().strip()
        host.monitor_usage = bool(self.chk_monitor_usage.isChecked())
        host.monitor_interval = int(self.spin_monitor_interval.value())
        poll = int(self.spin_poll_interval.value())
        # 1-4 s would be clamped up anyway; store what will actually happen.
        host.poll_interval = max(MIN_POLL_INTERVAL, poll) if poll else 0
        return host
