"""Submission resource, download and queue controls with explicit callbacks."""

from __future__ import annotations

from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from .submission_config import (
    BESIDE_INPUT_TEXT,
    CHAIN_ANY_TEXT,
    CHAIN_TEXT,
    DOWNLOAD_ALL_TEXT,
    FORCE_TEXT,
    with_reason,
)


class SubmissionResources(QWidget):
    def __init__(self, store, callbacks, parent=None):
        super().__init__(parent)
        self.store = store
        self._on_force_toggled = callbacks["_on_force_toggled"]
        self._on_scan_resources_toggled = callbacks["_on_scan_resources_toggled"]
        self._on_template_chosen = callbacks["_on_template_chosen"]
        self._refresh_preview = callbacks["_refresh_preview"]
        page = self
        form = QFormLayout(page)
        self.txt_queue = QLineEdit()
        self.txt_account = QLineEdit()
        self.txt_walltime = QLineEdit("24:00:00")
        self.spin_nodes = QSpinBox()
        self.spin_nodes.setRange(1, 1024)
        self.spin_ntasks = QSpinBox()
        self.spin_ntasks.setRange(1, 4096)
        self.spin_cpus = QSpinBox()
        self.spin_cpus.setRange(1, 512)
        self.txt_memory = QLineEdit()
        self.txt_memory.setPlaceholderText("e.g. 8G")
        self.txt_memory.setToolTip(
            "What the job needs in total. The built-in queue reserves it before starting."
        )
        self.chk_scan_resources = QCheckBox("Take these two from the input file")
        self.chk_scan_resources.setToolTip(
            "Take the cores and memory from the input file. Untick to type them by hand."
        )
        self.chk_scan_resources.setChecked(bool(self.store.get_pref("scan_resources", True)))
        self.chk_scan_resources.toggled.connect(self._on_scan_resources_toggled)
        self.spin_cpus.setEnabled(not self.chk_scan_resources.isChecked())
        self.txt_memory.setEnabled(not self.chk_scan_resources.isChecked())
        self.lbl_scanned = QLabel("")

        self.lbl_scanned.setWordWrap(True)
        self.lbl_scanned.setStyleSheet("color: palette(mid);")
        self.lbl_scanned.setVisible(False)
        self.txt_modules = QPlainTextEdit()
        self.txt_modules.setPlaceholderText("orca/5.0.4\nopenmpi/4.1.1")
        self.txt_modules.setMaximumHeight(60)
        self.txt_pre = QPlainTextEdit()
        self.txt_pre.setPlaceholderText("export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK")
        self.txt_pre.setMaximumHeight(60)
        self.txt_extra = QPlainTextEdit()
        self.txt_extra.setPlaceholderText("#SBATCH --exclusive")
        self.txt_extra.setMaximumHeight(60)
        self.txt_submit_options = QLineEdit()
        self.txt_submit_options.setPlaceholderText("e.g. -l select=1:ncpus=8 -W group_list=mygroup")
        self.txt_submit_options.setToolTip(
            "Arguments for sbatch / qsub itself, placed before the script:\n"
            "qsub <these> moleditpy_run.sh. Written as you would type them.\n"
            "The host's own submit options come first."
        )
        self.txt_submit_options.textChanged.connect(self._refresh_preview)
        self.txt_command = QLineEdit("orca {input} > {stem}.out")
        from .template_editor_dialog import PLACEHOLDER_TIP

        self.txt_command.setToolTip(PLACEHOLDER_TIP)
        self.txt_command.textChanged.connect(self._refresh_preview)
        self.cmb_template = QComboBox()
        self.cmb_template.setToolTip(
            "Conventional command line per program; picking one fills the Command field."
        )
        self.cmb_template.activated.connect(self._on_template_chosen)
        self.txt_globs = QLineEdit("*.out, *.log, *.xyz, *.hess, *.fchk")
        self.txt_globs.setToolTip("Which files come back when the job ends, comma separated.")
        self.chk_auto_download = QCheckBox("Download results automatically when the job ends")
        self.chk_auto_download.setChecked(bool(self.store.get_pref("auto_download", True)))
        self.chk_auto_download.toggled.connect(self._on_auto_download_toggled)

        self.chk_download_all = QCheckBox(DOWNLOAD_ALL_TEXT)
        self.chk_download_all.setToolTip(
            "Fetch everything the job produced, ignoring the patterns above."
        )
        self.chk_download_all.setChecked(bool(self.store.get_pref("download_all_outputs", True)))
        self.chk_download_all.toggled.connect(
            lambda checked: self.store.set_pref("download_all_outputs", bool(checked))
        )
        self.chk_download_all.setEnabled(self.chk_auto_download.isChecked())
        self.chk_beside_input = QCheckBox(BESIDE_INPUT_TEXT)
        self.chk_beside_input.setToolTip(
            "Put the results next to the input file instead of in the download folder."
        )
        self.chk_beside_input.setChecked(bool(self.store.get_pref("download_beside_input", True)))
        self.chk_beside_input.toggled.connect(
            lambda checked: self.store.set_pref("download_beside_input", bool(checked))
        )
        self.chk_beside_input.setEnabled(self.chk_auto_download.isChecked())

        self.txt_download_root = QLineEdit(self.store.get_pref("download_root", "") or "")
        self.txt_download_root.setPlaceholderText(self.store.download_root())
        self.txt_download_root.setToolTip(
            "Default download directory when results are not placed next to the input file."
        )
        # editingFinished, not textChanged: a preference write fsyncs, and
        # textChanged would fire one per keystroke.
        self.txt_download_root.editingFinished.connect(
            lambda: self.store.set_pref("download_root", self.txt_download_root.text().strip())
        )
        self.txt_download_root.setEnabled(self.chk_auto_download.isChecked())

        self.btn_browse_download_root = QPushButton("...")
        self.btn_browse_download_root.setMaximumWidth(32)
        self.btn_browse_download_root.setToolTip("Choose default download directory")
        self.btn_browse_download_root.clicked.connect(self._browse_download_root)
        self.btn_browse_download_root.setEnabled(self.chk_auto_download.isChecked())
        dl_root_row = QWidget()
        dl_root_layout = QHBoxLayout(dl_root_row)
        dl_root_layout.setContentsMargins(0, 0, 0, 0)
        dl_root_layout.addWidget(self.txt_download_root, 1)
        dl_root_layout.addWidget(self.btn_browse_download_root)

        self.chk_chain = QCheckBox(CHAIN_TEXT)
        self.chk_chain.setToolTip(
            "Hold this job until the one already queued on this host has finished."
        )
        self.chk_chain.setChecked(True)
        self.chk_chain_any = QCheckBox(CHAIN_ANY_TEXT)
        self.chk_chain_any.setToolTip(
            "Release it when that job ends, however it ended, rather than only on success."
        )
        self.chk_chain_any.toggled.connect(self._refresh_preview)
        self.lbl_chain = QLabel("")
        self.lbl_chain.setWordWrap(True)
        self.lbl_chain.setStyleSheet("color: palette(mid);")

        self.chk_start_at = QCheckBox("Do not start before")
        self.chk_start_at.setToolTip(
            "Hand the job over now, but do not let it start before this time."
        )
        self.dt_start_at = QDateTimeEdit()
        self.dt_start_at.setCalendarPopup(True)
        self.dt_start_at.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_start_at.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self.dt_start_at.setEnabled(False)
        self.chk_start_at.toggled.connect(self.dt_start_at.setEnabled)
        self.chk_start_at.toggled.connect(self._refresh_preview)
        self.dt_start_at.dateTimeChanged.connect(self._refresh_preview)
        start_row = QWidget()
        start_layout = QHBoxLayout(start_row)
        start_layout.setContentsMargins(0, 0, 0, 0)
        start_layout.addWidget(self.chk_start_at)
        start_layout.addWidget(self.dt_start_at, 1)

        # For a quick check that should not wait behind hours of work. Only
        # where this plugin is the queue: a cluster's scheduler decides its
        # own order, and nothing here can ask it to do otherwise.
        self.chk_force = QCheckBox(FORCE_TEXT)
        self.chk_force.setVisible(False)
        self.chk_force.toggled.connect(self._on_force_toggled)

        for widget in (
            self.txt_queue,
            self.txt_account,
            self.txt_walltime,
            self.txt_memory,
        ):
            widget.textChanged.connect(self._refresh_preview)
        for spin in (self.spin_nodes, self.spin_ntasks, self.spin_cpus):
            spin.valueChanged.connect(self._refresh_preview)
        for editor in (self.txt_modules, self.txt_pre, self.txt_extra):
            editor.textChanged.connect(self._refresh_preview)

        form.addRow("Queue / partition", self.txt_queue)
        form.addRow("Account", self.txt_account)
        form.addRow("Walltime", self.txt_walltime)
        form.addRow("Nodes", self.spin_nodes)
        form.addRow("Tasks", self.spin_ntasks)
        form.addRow("CPUs per task", self.spin_cpus)
        form.addRow("Memory", self.txt_memory)
        form.addRow(self.chk_scan_resources)
        form.addRow(self.lbl_scanned)
        form.addRow("Modules", self.txt_modules)
        form.addRow("Pre-commands", self.txt_pre)
        form.addRow("Extra directives", self.txt_extra)
        form.addRow("Submit options", self.txt_submit_options)
        self.command_row = QWidget()
        command_layout = QHBoxLayout(self.command_row)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.addWidget(self.txt_command, 1)
        command_layout.addWidget(self.cmb_template)
        form.addRow("Fetch patterns", self.txt_globs)
        form.addRow(self.chk_auto_download)
        form.addRow(self.chk_download_all)
        form.addRow(self.chk_beside_input)
        form.addRow("Default download dir", dl_root_row)
        form.addRow(self.chk_chain)
        form.addRow(self.chk_chain_any)
        form.addRow(self.lbl_chain)
        form.addRow(start_row)
        form.addRow(self.chk_force)

    def _browse_download_root(self) -> None:
        start = self.store.download_root()
        path = QFileDialog.getExistingDirectory(self, "Default Download Directory", start)
        if path:
            self.txt_download_root.setText(path)
            self.store.set_pref("download_root", path)

    def _on_auto_download_toggled(self, checked: bool) -> None:
        """Remember the choice and enable/disable all dependent download controls."""
        self.store.set_pref("auto_download", bool(checked))
        self.chk_download_all.setEnabled(checked)
        self.chk_beside_input.setEnabled(checked)
        off = "" if checked else "needs automatic download"
        self.chk_download_all.setText(with_reason(DOWNLOAD_ALL_TEXT, off))
        self.chk_beside_input.setText(with_reason(BESIDE_INPUT_TEXT, off))
        self.txt_download_root.setEnabled(checked)
        self.btn_browse_download_root.setEnabled(checked)
