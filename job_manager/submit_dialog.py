"""New Job dialog: pick a host, an input file and a submit preset."""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION, input_scan
from .command_templates import CommandTemplate, extension_of, suggest, templates_for
from .credentials import ensure_password

from .models import HostProfile, Job, SubmitPreset
from .runner import make_remote_dir
from .schedulers import get_scheduler, references_input
from .window_utils import make_independent
from .service import JobService

#: Offered by the file picker, in order. The first is the default until the
#: user picks another, after which their choice is what opens next time: a
#: person who submits Gaussian jobs all day should not scroll past every other
#: program's extensions every time.
INPUT_FILTERS = (
    "Calculation inputs (*.inp *.com *.gjf *.in *.xyz *.sh *.slurm)",
    "ORCA / CP2K / GAMESS (*.inp)",
    "Gaussian (*.com *.gjf)",
    "Quantum ESPRESSO / VASP / generic (*.in)",
    "Structures (*.xyz)",
    "Scripts (*.sh *.slurm *.pbs)",
    "All files (*)",
)
INPUT_FILTER = ";;".join(INPUT_FILTERS)

#: Dropdown entries that are actions rather than templates.
_SAVE_TEMPLATE = object()
_DELETE_TEMPLATE = object()
_SET_DEFAULT = object()
_MANAGE_TEMPLATES = object()


class SubmitDialog(QDialog):
    """Collects everything needed for one submission and previews the script."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.store = service.store
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Submit Job")
        make_independent(self)
        self.resize(760, self._preferred_height(640))
        # Input files can be dropped straight onto the wizard.
        self.setAcceptDrops(True)
        self._build_ui()
        self._reload_hosts()

    # --- prefilling ---------------------------------------------------------

    def prefill(
        self,
        files: Optional[List[str]] = None,
        name: str = "",
        host_id: str = "",
        preset: Optional[dict] = None,
        remote_dir: str = "",
        remote_input: str = "",
    ) -> None:
        """Populate the form from outside.

        Used by the input-generator handoff (a file that was just written) and
        by Resubmit (a previous job's host, preset and inputs).
        """
        if remote_dir:
            self.box_remote.setChecked(True)
            self.txt_remote_dir.setText(remote_dir)
            self.txt_remote_input.setText(remote_input)
        if host_id:
            index = self.cmb_host.findData(host_id)
            if index >= 0:
                self.cmb_host.setCurrentIndex(index)
        if preset:
            self._apply_preset(SubmitPreset.from_dict(preset))
        if files:
            self.list_files.clear()
            for path in files:
                if path:
                    self.list_files.addItem(path)
            first = os.path.dirname(files[0])
            if first:
                self.store.set_pref("last_input_dir", first)
            # After any preset above, so a resubmit keeps the numbers it ran
            # with: the scan only fills a field still at its default. Without
            # this the wizard read the input only when the file arrived through
            # "Add files...", and every other way in -- a drop, the input
            # generators' Submit to Cluster, Resubmit -- silently asked the
            # queue for one core.
            if not preset:
                self._apply_scanned_resources(files[0])
        if name:
            self.txt_job_name.setText(name)
        elif files:
            self.txt_job_name.setText(os.path.splitext(os.path.basename(files[0]))[0])
        elif remote_input:
            self.txt_job_name.setText(os.path.splitext(os.path.basename(remote_input))[0])
        self._reload_templates()
        if files and not preset:
            default_cmds = {"orca {input} > {stem}.out", "$(which orca) {input} > {stem}.out"}
            current_cmd = self.txt_command.text().strip()
            if not current_cmd or current_cmd in default_cmds:
                self._apply_suggested_template(force=True)
            else:
                self._apply_suggested_template(force=False)
        self._refresh_preview()

    # --- construction -------------------------------------------------------

    @staticmethod
    def _preferred_height(wanted: int) -> int:
        """``wanted``, or as much of the screen as there is.

        A laptop at 1366x768 has less usable height than this dialog wants, and
        a window taller than the screen puts Submit somewhere the mouse cannot
        reach. The body scrolls, so a short window loses nothing.
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return wanted
        return max(400, min(wanted, int(screen.availableGeometry().height() * 0.9)))

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        # Everything except the buttons scrolls: the resources tab alone is
        # taller than some screens, and the Submit button must stay reachable.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)

        top = QFormLayout()
        self.cmb_host = QComboBox()
        self.cmb_host.currentIndexChanged.connect(self._on_host_changed)
        self.cmb_preset = QComboBox()
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_changed)
        self.txt_job_name = QLineEdit()
        top.addRow("Host", self.cmb_host)
        top.addRow("Preset", self.cmb_preset)
        top.addRow("Job name", self.txt_job_name)
        layout.addLayout(top)

        files_box = QGroupBox(
            "Input files to upload - optional (the first one is passed to the command)"
        )
        files_box.setToolTip(
            "A job does not need one. Leave the list empty and the command runs "
            "on its own, in a new directory on the host -- for a script already "
            "installed there, or a command that makes its own input.\n\n"
            "A command that names {input} does need a file, either uploaded "
            "here or named under 'Work already on the host'."
        )
        files_layout = QVBoxLayout(files_box)
        self.list_files = QListWidget()
        files_layout.addWidget(self.list_files)
        row = QHBoxLayout()
        add = QPushButton("Add files...")
        add.clicked.connect(self._add_files)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_file)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        files_layout.addLayout(row)
        layout.addWidget(files_box)
        layout.addWidget(self._build_remote_box())

        tabs = QTabWidget()
        tabs.addTab(self._build_resources_tab(), "Resources")
        tabs.addTab(self._build_preview_tab(), "Script preview")
        layout.addWidget(tabs, 1)
        # Built with the resources tab, but shown up here: the command is what
        # the job *is*, and behind a tab of queue settings it read as though a
        # job were an input file and nothing else.
        top.addRow("Command", self.command_row)

        # Its own line, outside the scroll area and above Submit: saving a
        # preset is a different kind of act from submitting, and on one row with
        # them it was a button you could hit while reaching for Submit.
        preset_row = QHBoxLayout()
        self.btn_save_preset = QPushButton("Save as preset")
        self.btn_save_preset.clicked.connect(self._save_preset)
        preset_row.addWidget(self.btn_save_preset)
        preset_row.addStretch(1)
        outer.addLayout(preset_row)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Submit")
        box.accepted.connect(self._submit)
        box.rejected.connect(self.reject)
        # Outside the scroll area, so it stays put however far the body scrolls.
        outer.addWidget(box)

    def _build_remote_box(self) -> QWidget:
        """Point the job at work that is already on the host.

        The case this exists for: the files were staged on the cluster days
        ago -- generated there, copied with rsync, left over from a previous
        run -- and what is wanted from MoleditPy is only the submitting and
        the watching. Uploading a local copy of something already there is
        both pointless and, if the two have drifted, wrong.
        """
        box = QGroupBox("Work already on the host")
        box.setCheckable(True)
        box.setChecked(False)
        box.setToolTip(
            "Run the job in a directory that is already on the host, instead "
            "of in a new one made for it.\n\n"
            "Input files stay optional: with none, this submits a command "
            "over what is there. Any that are listed above are uploaded into "
            "that directory alongside it."
        )
        self.box_remote = box
        form = QFormLayout(box)

        self.txt_remote_dir = QLineEdit()
        self.txt_remote_dir.setPlaceholderText("~/runs/mol42")
        self.txt_remote_dir.setToolTip(
            "An absolute path, or one relative to your home directory on the "
            "host. It must exist: submitting checks, rather than creating it, "
            "so a typo is caught here instead of producing a job that runs in "
            "an empty directory."
        )
        self.txt_remote_input = QLineEdit()
        self.txt_remote_input.setPlaceholderText("mol.inp (optional)")
        self.txt_remote_input.setToolTip(
            "A file already in that directory, which {input} and {stem} then "
            "stand for -- so the usual command templates work unchanged.\n\n"
            "Leave it empty for a command that names its own files."
        )
        self.lbl_remote = QLabel("")
        self.lbl_remote.setWordWrap(True)
        self.lbl_remote.setStyleSheet("color: palette(mid);")

        check_row = QWidget()
        check_layout = QHBoxLayout(check_row)
        check_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_check_remote = QPushButton("Check")
        self.btn_check_remote.setToolTip("Ask the host what is in that directory.")
        self.btn_check_remote.clicked.connect(self._check_remote_dir)
        check_layout.addWidget(self.btn_check_remote)
        check_layout.addStretch(1)

        form.addRow("Directory", self.txt_remote_dir)
        form.addRow("Input file there", self.txt_remote_input)
        form.addRow("", check_row)
        form.addRow("", self.lbl_remote)

        box.toggled.connect(self._on_remote_toggled)
        self.txt_remote_dir.textChanged.connect(self._refresh_preview)
        self.txt_remote_input.textChanged.connect(self._refresh_preview)
        return box

    def _on_remote_toggled(self, checked: bool) -> None:
        if not checked:
            self.lbl_remote.setText("")
        self._refresh_preview()

    def remote_dir(self) -> str:
        """The host directory to run in, or "" for a new one per job."""
        if not self.box_remote.isChecked():
            return ""
        return self.txt_remote_dir.text().strip()

    def remote_input(self) -> str:
        """A file already on the host standing in for the uploaded input."""
        if not self.box_remote.isChecked():
            return ""
        return self.txt_remote_input.text().strip()

    def _check_remote_dir(self) -> None:
        host = self.current_host()
        path = self.txt_remote_dir.text().strip()
        if host is None or not path:
            self.lbl_remote.setText("Enter the directory first.")
            return
        if not ensure_password(self.service, host, self):
            return
        self.lbl_remote.setText(f"Looking at {path} on {host.name}...")
        self.btn_check_remote.setEnabled(False)

        def done(names: List[str]) -> None:
            self.btn_check_remote.setEnabled(True)
            wanted = self.txt_remote_input.text().strip()
            if not names:
                self.lbl_remote.setText(f"{path} is there, but has no files in it.")
                return
            shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
            text = f"{len(names)} file(s): {shown}"
            if wanted and wanted not in names:
                text += f"\nBut “{wanted}” is not one of them."
            self.lbl_remote.setText(text)

        def failed(message: str) -> None:
            self.btn_check_remote.setEnabled(True)
            self.lbl_remote.setText(message)

        self.service.list_remote_dir(host, path, done, failed)

    def _build_resources_tab(self) -> QWidget:
        page = QWidget()
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
            "What this job needs, in total.\n\n"
            "The helper queue on a host with no scheduler reserves it: the job "
            "starts when that much memory is free, so two 90 GB jobs do not "
            "both start on a 120 GB machine because the cores were free.\n\n"
            "Filled in from the input file where it says. Note that ORCA's "
            "%maxcore is per core, so it is multiplied by the core count."
        )
        self.chk_scan_resources = QCheckBox("Take these two from the input file")
        self.chk_scan_resources.setToolTip(
            "Read the core count and the memory request out of the input itself "
            "-- ORCA's %pal nprocs and %maxcore, Gaussian's %nprocshared and "
            "%mem, and the equivalents for Psi4, NWChem, Q-Chem and GAMESS.\n\n"
            "They are already written there, and the copy the queue schedules "
            "on is the one that gets forgotten: that is how two 90 GB jobs end "
            "up on a 120 GB machine.\n\n"
            "Untick to keep whatever you type here. Nothing is read, and adding "
            "another input file leaves both fields alone.\n\n"
            "Either way the input file itself is never edited, and the command "
            "line is not touched."
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
        self.txt_command = QLineEdit("orca {input} > {stem}.out")
        from .template_editor_dialog import PLACEHOLDER_TIP

        self.txt_command.setToolTip(PLACEHOLDER_TIP)
        self.txt_command.textChanged.connect(self._refresh_preview)
        self.cmb_template = QComboBox()
        self.cmb_template.setToolTip(
            "Conventional command line for each program MoleditPy writes input "
            "for. Picking one fills the Command field, which stays editable."
        )
        self.cmb_template.activated.connect(self._on_template_chosen)
        self._reload_templates()
        self.txt_globs = QLineEdit("*.out, *.log, *.xyz, *.hess, *.fchk")
        self.txt_globs.setToolTip(
            "Which files come back when the job ends. Comma separated.\n\n"
            "Picking a program from the Template dropdown fills these in for "
            "that program -- ORCA writes .gbw and .hess, Gaussian .chk and "
            ".fchk, VASP files with no extension at all -- and saving your own "
            "template keeps whatever you set here with it.\n\n"
            "A pattern may name a subdirectory: 'scratch/*.out' or '*/*.molden' "
            "fetch from one level down.\n\n"
            "The wrapper's own job.log is never downloaded whatever the "
            "patterns say; Tail Log reads it on the host."
        )
        #: What the last applied template put in the patterns field. An edit
        #: away from it means the user has an opinion, and the next template
        #: leaves the field alone.
        self._globs_before_template: list = []
        self.chk_auto_download = QCheckBox("Download results automatically when the job ends")
        self.chk_auto_download.setChecked(bool(self.store.get_pref("auto_download", True)))
        self.chk_auto_download.toggled.connect(self._on_auto_download_toggled)

        self.chk_download_all = QCheckBox("Download all output files")
        self.chk_download_all.setToolTip(
            "When checked, all output files produced by the job in its remote directory "
            "are downloaded upon completion without pattern filtering."
        )
        self.chk_download_all.setChecked(bool(self.store.get_pref("download_all_outputs", True)))
        self.chk_download_all.toggled.connect(
            lambda checked: self.store.set_pref("download_all_outputs", bool(checked))
        )
        self.chk_download_all.setEnabled(self.chk_auto_download.isChecked())

        self.chk_beside_input = QCheckBox("...next to the input file")
        self.chk_beside_input.setToolTip(
            "Put the results in the directory the input came from, which is "
            "where you are already working.\n\n"
            "Unticked, they go to a single download folder shared by every job "
            "(shown under Settings). A job with no local input to sit beside "
            "uses that folder either way.\n\n"
            "An input file is never overwritten by a result of the same name."
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
        self.txt_download_root.textChanged.connect(
            lambda text: self.store.set_pref("download_root", text.strip())
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

        self.chk_chain = QCheckBox("Run after the job already queued on this host")
        self.chk_chain.setToolTip(
            "Hold this job until the one already queued on this host has finished.\n\n"
            "On SLURM, PBS and SGE this becomes the queue's own dependency flag "
            "(--dependency=afterok, -W depend, -hold_jid). With no queue, the "
            "wrapper waits for the previous job's process instead -- which is "
            "the case that needs it most, since two submissions would otherwise "
            "start at once and fight over the same cores.\n\n"
            "Either way the waiting happens on the host, so the chain keeps "
            "moving with MoleditPy closed."
        )
        self.chk_chain.setChecked(True)
        self.chk_chain_any = QCheckBox("...even if that job fails")
        self.chk_chain_any.setToolTip(
            "SLURM and PBS are asked by default to start this job only if the "
            "previous one succeeds (afterok). That is usually right for a "
            "sequence -- an optimisation feeding a frequency job is pointless "
            "if the optimisation failed -- but it means one failure leaves "
            "everything behind it queued for ever.\n\n"
            "Tick this for a dependency the previous job satisfies simply by "
            "ending (afterany), when the jobs are independent and only being "
            "serialised to share the machine.\n\n"
            "SGE and the no-queue mode always release on the predecessor "
            "ending, so this changes nothing there."
        )
        self.chk_chain_any.toggled.connect(self._refresh_preview)
        self.lbl_chain = QLabel("")
        self.lbl_chain.setWordWrap(True)
        self.lbl_chain.setStyleSheet("color: palette(mid);")

        self.chk_start_at = QCheckBox("Do not start before")
        self.chk_start_at.setToolTip(
            "Hand the job over now, but tell the queue not to start it until "
            "this moment -- for a nightly window, or to stay off a shared "
            "machine during the day.\n\n"
            "Uses the scheduler's own flag (--begin, -a) where there is one, "
            "and waits in the wrapper where there is not. Either way the job is "
            "submitted immediately, so MoleditPy need not be running later."
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
        form.addRow("", self.chk_scan_resources)
        form.addRow("", self.lbl_scanned)
        form.addRow("Modules", self.txt_modules)
        form.addRow("Pre-commands", self.txt_pre)
        form.addRow("Extra directives", self.txt_extra)
        self.command_row = QWidget()
        command_layout = QHBoxLayout(self.command_row)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.addWidget(self.txt_command, 1)
        command_layout.addWidget(self.cmb_template)
        form.addRow("Fetch patterns", self.txt_globs)
        form.addRow("", self.chk_auto_download)
        form.addRow("", self.chk_download_all)
        form.addRow("", self.chk_beside_input)
        form.addRow("Default download dir", dl_root_row)
        form.addRow("", self.chk_chain)
        form.addRow("", self.chk_chain_any)
        form.addRow("", self.lbl_chain)
        form.addRow("", start_row)
        return page

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
        self.txt_download_root.setEnabled(checked)
        self.btn_browse_download_root.setEnabled(checked)

    def _build_preview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.lbl_preview_hint = QLabel(
            "This exact script is uploaded and submitted. The trailing sentinel is how "
            "the plugin detects completion."
        )
        self.lbl_preview_hint.setWordWrap(True)
        self.txt_preview = QPlainTextEdit()
        self.txt_preview.setReadOnly(True)
        layout.addWidget(self.lbl_preview_hint)
        layout.addWidget(self.txt_preview)
        return page

    # --- data ---------------------------------------------------------------

    def _reload_hosts(self) -> None:
        self.cmb_host.blockSignals(True)
        self.cmb_host.clear()
        for host in self.store.host_list():
            self.cmb_host.addItem(f"{host.name} ({host.target})", host.id)
        self.cmb_host.blockSignals(False)
        self._on_host_changed()

    def current_host(self) -> Optional[HostProfile]:
        return self.store.hosts.get(self.cmb_host.currentData())

    def _on_host_changed(self) -> None:
        host = self.current_host()
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.clear()
        self.cmb_preset.addItem("(new preset)", "")
        if host is not None:
            for preset in self.store.presets_for_host(host.id):
                self.cmb_preset.addItem(preset.name, preset.id)
        self.cmb_preset.blockSignals(False)
        if self.cmb_preset.count() > 1:
            self.cmb_preset.setCurrentIndex(1)
        self._on_preset_changed()
        self._update_queue_fields()
        if host is not None and self.cmb_preset.currentIndex() == 0:
            # Only where no named preset took the form: a preset the user chose
            # is a decision, and last time's settings must not overwrite it.
            # Do not apply remembered preset so the form resets to default each time.
            # self._apply_remembered(host)
            pass
        self._update_chain_row()

    def _update_queue_fields(self) -> None:
        """Grey the fields this host's scheduler has no queue to read.

        A walltime typed for a machine with no queue is not enforced by
        anything, and an account for a machine with no accounting is a line in
        a script nobody reads. Cores and memory stay live: the helper queue
        schedules on them, and the command template can spell them.
        """
        host = self.current_host()
        try:
            scheduler = get_scheduler(host.scheduler) if host else None
        except ValueError:
            scheduler = None
        live = scheduler is None or scheduler.queue_directives
        for widget in (
            self.txt_queue,
            self.txt_account,
            self.txt_walltime,
            self.spin_nodes,
            self.txt_extra,
        ):
            widget.setEnabled(live)
            if not live:
                widget.setToolTip(
                    f"{scheduler.label if scheduler else 'This host'} has no queue to read it: "
                    "there is no partition to pick, no account to charge and no "
                    "walltime anything will enforce.\n\n"
                    "Cores and memory still matter -- the helper queue schedules "
                    "on them."
                )

    #: How each scheduler is told to wait, for the hint under the checkbox.
    _CHAIN_MECHANISM = {
        "slurm": "--dependency=afterok",
        "pbs": "-W depend=afterok",
        "sge": "-hold_jid",
        "shell": "the wrapper waits for its process",
    }

    def slot_limit(self) -> int:
        """How many jobs this host may run at once; 0 for no limit."""
        host = self.current_host()
        if host is None:
            return 0
        return max(0, int(getattr(host, "max_concurrent", 0) or 0))

    def _update_chain_row(self) -> None:
        """Every scheduler can chain; only the mechanism differs."""
        host = self.current_host()
        predecessor = self.chain_predecessor()
        try:
            scheduler = get_scheduler(host.scheduler) if host else None
        except ValueError:
            scheduler = None
        chainable = scheduler is not None and scheduler.supports_chaining
        limit = self.slot_limit()

        # A limit the user can untick is not a limit, so where one is set the
        # host decides and the manual controls step aside.
        self.chk_chain.setVisible(chainable and not limit)
        self.chk_chain.setEnabled(chainable and not limit and predecessor is not None)
        # Only worth offering where the two forms differ: SGE and the no-queue
        # mode release on the predecessor ending whatever happened to it.
        conditional = chainable and not limit and not scheduler.chain_releases_on_failure
        self.chk_chain_any.setVisible(conditional)
        self.chk_chain_any.setEnabled(conditional and self.chk_chain.isEnabled())
        self.lbl_chain.setVisible(chainable)
        if not chainable:
            return

        if limit:
            running = len(self.store.chain_lanes(host.id))
            if predecessor is None:
                self.lbl_chain.setText(
                    f"{host.name} runs at most {limit} at a time "
                    f"({running} of {limit} in use): this job starts straight away."
                )
            else:
                self.lbl_chain.setText(
                    f"{host.name} runs at most {limit} at a time (all {limit} in use): "
                    f"this job waits for “{predecessor.name}” and starts when that slot frees."
                )
            return

        if predecessor is None:
            self.lbl_chain.setText("Nothing queued on this host: this job starts straight away.")
            return
        how = self._CHAIN_MECHANISM.get(host.scheduler, "") if host else ""
        self.lbl_chain.setText(
            f"Would start after “{predecessor.name}” "
            f"({predecessor.remote_job_id or 'not yet submitted'}) finishes"
            + (f", via {how}." if how else ".")
        )

    def chain_requested(self) -> bool:
        """One predicate for both the preview and the submission.

        They asked slightly different questions before, so the Script preview
        could show a dependency that submitting would not actually apply.
        """
        if self.slot_limit():
            return self.chain_predecessor() is not None
        # isHidden(), not isVisible(): a widget on a tab the user has switched
        # away from is not "visible", so reading isVisible() here dropped the
        # dependency for anyone who checked the Script preview tab before
        # pressing Submit -- which is precisely what that tab invites.
        return (
            not self.chk_chain.isHidden()
            and self.chk_chain.isEnabled()
            and self.chk_chain.isChecked()
            and self.chain_predecessor() is not None
        )

    def chain_any_requested(self) -> bool:
        """True for an ``afterany`` dependency rather than ``afterok``."""
        if self.slot_limit():
            # A slot limit serialises jobs that have nothing to do with each
            # other. Holding them on afterok would let one failure strand a
            # whole lane, which is the opposite of what a limit is for.
            return self.chain_requested()
        return (
            self.chain_requested()
            and not self.chk_chain_any.isHidden()
            and self.chk_chain_any.isEnabled()
            and self.chk_chain_any.isChecked()
        )

    def selected_start_time(self) -> float:
        """Epoch second the job must not start before, or 0 for "now"."""
        if not self.chk_start_at.isChecked():
            return 0.0
        return float(self.dt_start_at.dateTime().toSecsSinceEpoch())

    def chain_predecessor(self) -> Optional["Job"]:
        """The job this one would queue behind, or None to start now."""
        host = self.current_host()
        if host is None:
            return None
        limit = self.slot_limit()
        if limit:
            # Join the shortest lane, so a limit of two and seven submissions
            # becomes two balanced queues rather than one long chain.
            return self.store.chain_lane_tail(host.id, limit)
        return self.store.chain_tail(host.id)

    def _on_preset_changed(self) -> None:
        preset = self.store.presets.get(self.cmb_preset.currentData())
        if preset is not None:
            self._apply_preset(preset)
        self._refresh_preview()

    def _apply_preset(self, preset: SubmitPreset) -> None:
        self.txt_queue.setText(preset.queue)
        self.txt_account.setText(preset.account)
        self.txt_walltime.setText(preset.walltime)
        self.spin_nodes.setValue(int(preset.nodes or 1))
        self.spin_ntasks.setValue(int(preset.ntasks or 1))
        self.spin_cpus.setValue(int(preset.cpus_per_task or 1))
        self.txt_memory.setText(preset.memory)
        self.txt_modules.setPlainText("\n".join(preset.modules or []))
        self.txt_pre.setPlainText("\n".join(preset.pre_commands or []))
        self.txt_extra.setPlainText("\n".join(preset.extra_directives or []))
        self.txt_command.setText(preset.command_template)
        self.txt_globs.setText(", ".join(preset.fetch_globs or []))
        if preset.name in ("default", ""):
            self.chk_auto_download.setChecked(bool(self.store.get_pref("auto_download", True)))
        else:
            self.chk_auto_download.setChecked(bool(preset.auto_download))

    def collect_preset(self) -> SubmitPreset:
        host = self.current_host()
        globs = [g.strip() for g in self.txt_globs.text().split(",") if g.strip()]

        preset = SubmitPreset(
            host_id=host.id if host else "",
            name=self.cmb_preset.currentText() or "default",
            queue=self.txt_queue.text().strip(),
            account=self.txt_account.text().strip(),
            walltime=self.txt_walltime.text().strip(),
            nodes=int(self.spin_nodes.value()),
            ntasks=int(self.spin_ntasks.value()),
            cpus_per_task=int(self.spin_cpus.value()),
            memory=self.txt_memory.text().strip(),
            modules=[m.strip() for m in self.txt_modules.toPlainText().splitlines() if m.strip()],
            pre_commands=[c.strip() for c in self.txt_pre.toPlainText().splitlines() if c.strip()],
            extra_directives=[
                d.strip() for d in self.txt_extra.toPlainText().splitlines() if d.strip()
            ],
            command_template=self.txt_command.text(),
            fetch_globs=globs,
            auto_download=bool(self.chk_auto_download.isChecked()),
        )
        return preset

    # --- command templates --------------------------------------------------

    def _reload_templates(self) -> None:
        """Refill the dropdown, most likely program first for these inputs."""
        files = self.selected_files() if hasattr(self, "list_files") else []
        self.cmb_template.blockSignals(True)
        self.cmb_template.clear()
        self.cmb_template.addItem("Template...", None)

        for template in templates_for(os.path.basename(files[0]) if files else ""):
            self.cmb_template.addItem(template.label, template)
            if template.note:
                self.cmb_template.setItemData(
                    self.cmb_template.count() - 1,
                    f"{template.command or '(type your own)'}\n\n{template.note}",
                    Qt.ItemDataRole.ToolTipRole,
                )

        saved = self.store.user_templates()
        if saved:
            self.cmb_template.insertSeparator(self.cmb_template.count())
            for entry in saved:
                self.cmb_template.addItem(
                    entry["label"],
                    CommandTemplate(
                        entry["label"],
                        entry["command"],
                        fetch_globs=tuple(entry.get("fetch_globs") or ()),
                    ),
                )
                self.cmb_template.setItemData(
                    self.cmb_template.count() - 1,
                    entry["command"],
                    Qt.ItemDataRole.ToolTipRole,
                )

        self.cmb_template.insertSeparator(self.cmb_template.count())
        extension = extension_of(os.path.basename(files[0])) if files else ""
        if extension:
            # The answer to an ambiguous extension. ORCA, CP2K and GAMESS all
            # write .inp, so the wizard will not guess -- but it will remember
            # which one this user means.
            self.cmb_template.addItem(f"Use this command for every {extension}", _SET_DEFAULT)
        self.cmb_template.addItem("Save current command as...", _SAVE_TEMPLATE)
        if saved:
            self.cmb_template.addItem("Delete a saved template...", _DELETE_TEMPLATE)
        self.cmb_template.addItem("Manage templates...", _MANAGE_TEMPLATES)
        self.cmb_template.blockSignals(False)

    def _on_template_chosen(self, index: int) -> None:
        choice = self.cmb_template.itemData(index)
        self.cmb_template.setCurrentIndex(0)
        if choice is _SAVE_TEMPLATE:
            self._save_user_template()
        elif choice is _SET_DEFAULT:
            self._set_default_for_extension()
        elif choice is _DELETE_TEMPLATE:
            self._delete_user_template()
        elif choice is _MANAGE_TEMPLATES:
            self._manage_templates()
        elif choice is not None:
            self.txt_command.setText(choice.command)
            self._apply_template_globs(choice)

    def _apply_template_globs(self, template: CommandTemplate) -> None:
        """Take the fetch patterns from the program that was just chosen.

        A pattern list is about the program, not the molecule: ORCA writes
        .gbw and .hess, Gaussian .chk and .fchk, VASP files with no extension
        at all. The wizard's one-size list quietly downloaded nothing for half
        of them.

        Never over a list the user has edited away from what it was, for the
        same reason the command is not: a filled field is a decision.
        """
        if not template.fetch_globs:
            return
        current = [g.strip() for g in self.txt_globs.text().split(",") if g.strip()]
        if current and current != self._globs_before_template:
            return
        self.txt_globs.setText(", ".join(template.fetch_globs))
        self._globs_before_template = list(template.fetch_globs)

    def _set_default_for_extension(self) -> None:
        """Make this command what an input of that extension gets from now on."""
        files = self.selected_files()
        extension = extension_of(os.path.basename(files[0])) if files else ""
        command = self.txt_command.text().strip()
        if not extension:
            return
        if not command:
            QMessageBox.information(self, "Default command", "Enter a command first.")
            return
        globs = [g.strip() for g in self.txt_globs.text().split(",") if g.strip()]
        self.store.set_default_command(extension, command, globs)
        QMessageBox.information(
            self,
            "Default command",
            f"Every {extension} added from now on starts with this command"
            + (" and these fetch patterns." if globs else "."),
        )
        self._reload_templates()

    def _save_user_template(self) -> None:
        command = self.txt_command.text().strip()
        if not command:
            QMessageBox.information(self, "Save template", "Enter a command first.")
            return
        label, accepted = QInputDialog.getText(
            self, "Save template", "Name for this command template:"
        )
        if not accepted or not label.strip():
            return
        globs = [g.strip() for g in self.txt_globs.text().split(",") if g.strip()]
        # The patterns are part of what makes a template useful: a saved
        # command for a program whose results you then have to re-list by hand
        # is half a template.
        self.store.add_user_template(label.strip(), command, globs)
        self._reload_templates()

    def _delete_user_template(self) -> None:
        labels = [entry["label"] for entry in self.store.user_templates()]
        if not labels:
            return
        label, accepted = QInputDialog.getItem(
            self, "Delete template", "Remove which template?", labels, 0, False
        )
        if accepted and label:
            self.store.remove_user_template(label)
            self._reload_templates()

    def _manage_templates(self) -> None:
        from .template_editor_dialog import TemplateEditorDialog

        TemplateEditorDialog(self.store, self).exec()
        self._reload_templates()

    def _apply_suggested_template(self, force: bool = False) -> None:
        """Fill an empty command from the input's extension; if force=True, overwrites."""
        files = self.selected_files()
        if not files:
            return
        if not force and self.txt_command.text().strip():
            return
        filename = os.path.basename(files[0])
        ext = extension_of(filename)
        # The user's own answer first: they have said which program writes
        # this extension, which is more than the built-in list can know.
        stored = self.store.default_command_for(ext)
        if stored.get("command"):
            self.txt_command.setText(stored["command"])
            self._apply_template_globs(
                CommandTemplate(
                    "", stored["command"], fetch_globs=tuple(stored.get("fetch_globs") or ())
                )
            )
            return
        template = suggest(filename)
        if template is not None and template.command:
            self.txt_command.setText(template.command)
            self._apply_template_globs(template)
            for i in range(self.cmb_template.count()):
                if self.cmb_template.itemText(i) == template.label:
                    self.cmb_template.setCurrentIndex(i)
                    break

    # --- files --------------------------------------------------------------

    def selected_files(self) -> List[str]:
        return [self.list_files.item(row).text() for row in range(self.list_files.count())]

    def _add_files(self) -> None:
        start = self.store.get_pref("last_input_dir", "") or ""
        chosen = self.store.get_pref("input_filter", "") or INPUT_FILTERS[0]
        if chosen not in INPUT_FILTERS:
            # A filter from a version that offered different ones. Qt would
            # simply show nothing selected, so fall back rather than puzzle.
            chosen = INPUT_FILTERS[0]
        paths, used = QFileDialog.getOpenFileNames(
            self, "Select input files", start, INPUT_FILTER, chosen
        )
        if used:
            self.store.set_pref("input_filter", used)
        self.add_files(paths or [])

    def add_files(self, paths: Sequence[str]) -> None:
        """Add input files from the picker or from a drop, and follow up.

        The follow-up is the point: naming the job, offering the right command
        template for the extension, and refreshing the preview are what make an
        added file useful rather than just listed.
        """
        added = [p for p in paths or [] if p and p not in self.selected_files()]
        for path in added:
            self.list_files.addItem(path)
        if added:
            self.store.set_pref("last_input_dir", os.path.dirname(added[0]))
            if not self.txt_job_name.text().strip():
                self.txt_job_name.setText(os.path.splitext(os.path.basename(added[0]))[0])
            self._apply_scanned_resources(added[0])
        self._reload_templates()
        self._apply_suggested_template()
        self._refresh_preview()

    def _apply_scanned_resources(self, path: str) -> None:
        """Fill Memory and CPUs from what the input file already asks for.

        The user has typed those numbers once, into the input. Asking for them
        again is asking them to keep two copies of one fact in step -- and the
        copy the queue schedules on is the one that gets forgotten, which is
        how two 90 GB jobs end up on a 120 GB machine.

        Only while the box under the two fields is ticked, and only into a
        field that is still at its default: a value already there is a
        decision, and a guess must not silently replace it. Untick the box to
        keep the fields entirely by hand.

        The command line is never touched by any of this, and neither is the
        input file.
        """
        if not self.chk_scan_resources.isChecked():
            return
        found = input_scan.scan(path)
        if not found.found:
            return
        filled = []
        if found.memory_mb and not self.txt_memory.text().strip():
            self.txt_memory.setText(input_scan.format_memory(found.memory_mb))
            filled.append(f"memory {input_scan.format_memory(found.memory_mb)}")
        if found.cores > 0 and self.spin_cpus.value() <= 1:
            self.spin_cpus.setValue(found.cores)
            filled.append(f"{found.cores} CPUs")
        if filled:
            self.lbl_scanned.setText(
                f"Read from the {found.program} input: {', '.join(filled)}. "
                "Untick to enter them by hand."
            )
            self.lbl_scanned.setVisible(True)

    def _on_scan_resources_toggled(self, checked: bool) -> None:
        """Remember the choice, disable/enable inputs, and act on it for the file already chosen."""
        self.store.set_pref("scan_resources", bool(checked))
        self.spin_cpus.setEnabled(not checked)
        self.txt_memory.setEnabled(not checked)
        if not checked:
            self.lbl_scanned.setVisible(False)
            return
        files = self.selected_files()
        if files:
            self._apply_scanned_resources(files[0])

    # --- drops ---------------------------------------------------------------

    @staticmethod
    def dropped_files(event) -> List[str]:
        """Local file paths in a drag event; empty for anything else."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        # toLocalFile() returns forward slashes on Windows, which every path
        # here is compared against, so normalise once.
        return [
            os.path.normpath(url.toLocalFile())
            for url in mime.urls()
            if url.isLocalFile() and os.path.isfile(url.toLocalFile())
        ]

    def dragEnterEvent(self, event) -> None:
        if self.dropped_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event) -> None:
        paths = self.dropped_files(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.add_files(paths)

    def _remove_file(self) -> None:
        for item in self.list_files.selectedItems():
            self.list_files.takeItem(self.list_files.row(item))
        self._refresh_preview()

    # --- preview ------------------------------------------------------------

    def _refresh_preview(self) -> None:
        host = self.current_host()
        if host is None:
            self.txt_preview.setPlainText("Add a host profile first (Hosts...).")
            return
        files = self.selected_files()
        input_name = self.remote_input() or (os.path.basename(files[0]) if files else "")
        if not input_name and not self.remote_dir():
            # Nothing chosen yet: show what a job with an input would look
            # like, rather than a script with an empty command in it.
            input_name = "input.inp"
        try:
            scheduler = get_scheduler(host.scheduler)
        except ValueError as exc:
            self.txt_preview.setPlainText(str(exc))
            return
        predecessor = self.chain_predecessor() if self.chain_requested() else None
        name = self.txt_job_name.text().strip() or "moleditpy_job"
        script = scheduler.build_script(
            name,
            self.collect_preset(),
            input_name,
            "job.log",
            run_after=(predecessor.remote_job_id if predecessor else ""),
            run_after_any=self.chain_any_requested(),
            start_after=self.selected_start_time(),
            # Built the same way submitting will build it, so the preview shows
            # the directory the script really cds into (bar the timestamp).
            remote_dir=self.remote_dir() or make_remote_dir(host, name),
            # The preview is worth having only if it is the script that runs,
            # and the host's environment setup is part of that script.
            preamble=host.environment_commands(),
        )
        self.txt_preview.setPlainText(script)

    # --- actions ------------------------------------------------------------

    def _save_preset(self) -> None:
        host = self.current_host()
        if host is None:
            return
        preset = self.collect_preset()
        existing_id = self.cmb_preset.currentData()
        if existing_id:
            preset.id = existing_id
            preset.name = self.cmb_preset.currentText()
        else:
            name = self.txt_job_name.text().strip() or "preset"
            preset.name = name
        self.store.add_preset(preset)
        self._on_host_changed()
        index = self.cmb_preset.findData(preset.id)
        if index >= 0:
            self.cmb_preset.setCurrentIndex(index)

    def _submit(self) -> None:
        host = self.current_host()
        if host is None:
            QMessageBox.warning(self, "Submit", "Add a host profile first.")
            return
        files = self.selected_files()
        missing = [path for path in files if not os.path.isfile(path)]
        if missing:
            QMessageBox.warning(self, "Submit", f"File not found:\n{missing[0]}")
            return
        preset = self.collect_preset()
        if not preset.command_template.strip():
            QMessageBox.warning(self, "Submit", "Enter the command to run.")
            return
        remote_dir = self.remote_dir()
        if self.box_remote.isChecked() and not remote_dir:
            QMessageBox.warning(
                self, "Submit", "Enter the directory on the host, or untick the box."
            )
            return
        if not files and not self.remote_input() and references_input(preset.command_template):
            # The template still names an input this job does not have, so it
            # would substitute to `orca  > .out` and fail on the host. Caught
            # here rather than in tomorrow's log.
            QMessageBox.warning(
                self,
                "Submit",
                "The command uses {input}, but this job has no input file.\n\n"
                "Add one above, name one under “Work already on the host”, or "
                "write a command that does not refer to an input.",
            )
            return
        if not files and not remote_dir:
            # Not an error -- a command that needs no input of its own is a
            # real job -- but it is far more often a forgotten file.
            confirm = QMessageBox.question(
                self,
                "Submit",
                "No input files, and no directory on the host.\n\n"
                "The command will run in a new, empty directory. Submit anyway?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        if not self._confirm_duplicate(files):
            return
        if not ensure_password(self.service, host, self):
            return
        name = self.txt_job_name.text().strip() or self._default_job_name(files, remote_dir)
        after = self.chain_predecessor() if self.chain_requested() else None
        self.service.submit(
            host,
            preset,
            name,
            files,
            after_job=after,
            start_after=self.selected_start_time(),
            chain_any=self.chain_any_requested(),
            remote_dir=remote_dir,
            remote_input=self.remote_input(),
        )
        self._remember(host, preset)
        self.accept()

    def _remember(self, host: HostProfile, preset: SubmitPreset) -> None:
        """Keep this submission's settings as the starting point for the next.

        Presets are the named, deliberate version of this; a person who submits
        the same kind of job every day should not have to name one to stop
        retyping the walltime, the modules and the fetch patterns each time.
        """
        remembered = dict(self.store.get_pref("last_preset", {}) or {})
        remembered[host.id] = preset.to_dict()
        self.store.set_pref("last_preset", remembered)

    def _apply_remembered(self, host: HostProfile) -> None:
        """Restore the last submission to this host, where nothing else has."""
        data = (self.store.get_pref("last_preset", {}) or {}).get(host.id)
        if not data:
            return
        self._apply_preset(SubmitPreset.from_dict(data))
        if self.chk_scan_resources.isChecked():
            # Those two describe the molecule, not the site: with the box
            # ticked they come from the input file, so they are put back to
            # their defaults for the scan to fill. Unticked, last time's
            # numbers are exactly what was wanted.
            self.spin_cpus.setValue(1)
            self.txt_memory.setText("")

    def _confirm_duplicate(self, files: List[str]) -> bool:
        """Warn when this input has been submitted before. False cancels.

        Not for Resubmit, which is somebody asking for exactly that. This is
        the accident: the same file sent twice from the wizard, which on a
        no-queue host means two copies of one calculation fighting over the
        same cores, and on a cluster means paying twice for one answer.
        """
        wanted = {os.path.abspath(path) for path in files if path}
        if not wanted:
            return True
        clashes = [
            job
            for job in self.store.job_list()
            if wanted & {os.path.abspath(p) for p in (job.input_files or []) if p}
        ]
        if not clashes:
            return True
        first = clashes[0]
        running = [job for job in clashes if job.is_active]
        detail = (
            f"'{first.name}' is {first.state.lower()} on {first.host_name or 'a host'}"
            if running
            else f"'{first.name}' was submitted before ({first.state.lower()})"
        )
        if len(clashes) > 1:
            detail += f", and {len(clashes) - 1} more"
        answer = QMessageBox.question(
            self,
            "Already submitted",
            f"{os.path.basename(files[0])} has been submitted from here before.\n\n"
            f"{detail}.\n\nSubmit it again?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _default_job_name(self, files: List[str], remote_dir: str) -> str:
        """What the job is called when the user did not name it."""
        if files:
            return os.path.basename(files[0])
        if self.remote_input():
            return os.path.basename(self.remote_input())
        if remote_dir:
            return os.path.basename(remote_dir.rstrip("/\\")) or "job"
        return "job"
