"""New Job dialog: pick a host, an input file and a submit preset."""

from __future__ import annotations

import os
from typing import ClassVar, List, Optional, Sequence

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

from . import structure_relay
from .models import HostProfile, Job, SubmitPreset
from .runner import make_remote_dir
from .schedulers import get_scheduler, references_input
from .theme import apply_theme
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
        # The one window that had never had it, so its buttons and fields were
        # a different size and colour from every other window in the plugin.
        apply_theme(self)
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
        batch: bool = False,
    ) -> None:
        """Populate the form from outside.

        Used by the input-generator handoff (a file that was just written) and
        by Resubmit (a previous job's host, preset and inputs). ``batch`` asks
        for the "one job per file" checkbox to start ticked -- for a drop of
        several files at once, where each is its own calculation rather than
        one job's worth of inputs.
        """
        if remote_dir:
            self.box_remote.setChecked(True)
            self.txt_remote_dir.setText(remote_dir)
            self.txt_remote_input.setText(remote_input)
        if not host_id and files:
            # An input written into the share that mirrors a host's filesystem
            # is already on that host as far as the user is concerned, so that
            # is the host to open on -- ahead of whichever one was used last.
            owner = self.store.host_for_local_path(files[0])
            if owner is not None:
                host_id = owner.id
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
        self._update_batch_row()
        self._update_relay_row()
        if batch:
            # Only takes effect where it is enabled -- more than one file, and
            # not "work already on the host", which names a single file.
            self.chk_batch.setChecked(True)
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
            "Optional. With none, the command runs on its own in a new directory on the host."
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
        self.chk_batch = QCheckBox("Submit each file as its own job")
        self.chk_batch.setToolTip(
            "One independent job per file, each running the command below on "
            "its own -- rather than one job with every file uploaded to it.\n\n"
            "Shown only with more than one file, and not together with 'Work "
            "already on the host', which names a single file for {input}.\n\n"
            "Dropping several files onto this window or the monitor starts "
            "ticked; hold Shift while dropping to keep them as one job instead."
        )
        self.chk_batch.toggled.connect(self._on_batch_toggled)
        self.chk_batch.setVisible(False)
        files_layout.addWidget(self.chk_batch)
        layout.addWidget(files_box)
        layout.addWidget(self._build_remote_box())
        layout.addWidget(self._build_structure_relay_box())

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
        self.btn_submit = box.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_submit.setText("Submit")
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
            "Run the job in a directory that is already on the host, instead of a new one."
        )
        self.box_remote = box
        form = QFormLayout(box)

        self.txt_remote_dir = QLineEdit()
        self.txt_remote_dir.setPlaceholderText("~/runs/mol42")
        self.txt_remote_dir.setToolTip(
            "Absolute, or relative to your home on the host. It has to exist already."
        )
        self.txt_remote_input = QLineEdit()
        self.txt_remote_input.setPlaceholderText("mol.inp (optional)")
        self.txt_remote_input.setToolTip(
            "A file in that directory, which {input} and {stem} then stand for. Optional."
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
        self._update_batch_row()
        self._update_relay_row()
        self._refresh_preview()

    # --- structure relay -----------------------------------------------------

    def _build_structure_relay_box(self) -> QWidget:
        """Fill ``[prevfile]``/``[prevfile:.ext]`` tags with a previous job's
        own files, copied in on the host itself.

        What the tag means to the program that reads it -- an ORCA
        ``* xyzfile``, a Gaussian ``%oldchk`` -- is between the input file and
        that program; nothing here parses chemistry or knows which software
        wrote either job. It only ever relays between two jobs on the *same*
        host, with a single remote copy per file and nothing downloaded to
        this machine and re-uploaded.
        """
        box = QGroupBox("Reuse another job's file")
        box.setCheckable(True)
        box.setChecked(False)
        box.setToolTip(
            "Copy a file from another job on this host into this one, and "
            "write its name wherever the input above says "
            f"{structure_relay.TAG_RE.pattern} -- an ORCA geometry, a "
            "Gaussian checkpoint, anything the input names by an extension.\n\n"
            "A job that has not finished yet can be picked too: this one is "
            "then chained to start only once it succeeds, so the file is "
            "there by the time the copy runs.\n\n"
            "Only jobs on the host selected above are offered: a file cannot "
            "be relayed onto a different machine without downloading and "
            "re-uploading it, which this does not do.\n\n"
            "Needs an input file, and neither batch mode nor 'Work already "
            "on the host', which have no single local file this could rewrite."
        )
        self.box_relay = box
        # Nothing to relay onto until there is an input file; every later
        # state is reached through _update_relay_row(), called from
        # add_files/prefill/etc, none of which have run yet.
        box.setEnabled(False)
        form = QFormLayout(box)

        self.cmb_relay_source = QComboBox()
        self.cmb_relay_source.currentIndexChanged.connect(self._update_relay_status)
        form.addRow("Source job", self.cmb_relay_source)

        self.lbl_relay_status = QLabel("")
        self.lbl_relay_status.setWordWrap(True)
        self.lbl_relay_status.setStyleSheet("color: palette(mid);")
        form.addRow("", self.lbl_relay_status)

        box.toggled.connect(self._on_relay_toggled)
        return box

    def _update_relay_row(self) -> None:
        """The relay box needs exactly one local file and nothing exotic.

        Reads ``chk_batch.isChecked()`` directly, not ``_batch_active()``:
        that also asks whether the box is *enabled*, and the two rows disable
        each other, so mid-toggle one of them is checked but momentarily
        disabled -- exactly the state each row's own guard has to see through
        to break the tie cleanly rather than leaving both re-enabled.
        """
        allowed = bool(self.selected_files()) and not self.box_remote.isChecked()
        allowed = allowed and not self.chk_batch.isChecked()
        self.box_relay.setEnabled(allowed)
        if not allowed and self.box_relay.isChecked():
            self.box_relay.blockSignals(True)
            self.box_relay.setChecked(False)
            self.box_relay.blockSignals(False)

    def _on_relay_toggled(self, checked: bool) -> None:
        if checked:
            self._reload_relay_sources()
            self._update_batch_row()
        self._refresh_preview()

    def _reload_relay_sources(self) -> None:
        """Refill the source-job dropdown with candidate jobs on this host."""
        host = self.current_host()
        current = self.cmb_relay_source.currentData()
        self.cmb_relay_source.blockSignals(True)
        self.cmb_relay_source.clear()
        jobs = structure_relay.candidate_jobs(
            self.store.jobs.values(), host_id=host.id if host else ""
        )
        for job in jobs:
            self.cmb_relay_source.addItem(job.name, job.id)
        if current:
            index = self.cmb_relay_source.findData(current)
            if index >= 0:
                self.cmb_relay_source.setCurrentIndex(index)
        self.cmb_relay_source.blockSignals(False)
        self._update_relay_status()

    def selected_relay_job(self) -> Optional[Job]:
        job_id = self.cmb_relay_source.currentData()
        return self.store.jobs.get(job_id) if job_id else None

    def _update_relay_status(self) -> None:
        """What will actually be filled in, read from the input as it stands."""
        job = self.selected_relay_job()
        if job is None:
            self.lbl_relay_status.setText(
                "" if self.store.jobs else "No other job on this host yet."
            )
            return
        files = self.selected_files()
        if not files:
            self.lbl_relay_status.setText("")
            return
        try:
            with open(files[0], "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            self.lbl_relay_status.setText("")
            return
        tags = structure_relay.find_tags(text)
        if not tags:
            self.lbl_relay_status.setText(f"No {structure_relay.TAG_RE.pattern} tag found yet.")
            return
        resolved = sorted(
            {
                structure_relay.resolve_filename(job, match.group("ext"))
                for match in tags
                if match.group("ext")
            }
        )
        self.lbl_relay_status.setText(
            f"Will copy {', '.join(resolved)} from {job.name}." if resolved else ""
        )

    @staticmethod
    def _files_with_relay_tags(paths: Sequence[str]) -> List[str]:
        """Those of ``paths`` still carrying an unresolved ``[prevfile]`` tag.

        Read rather than assumed: a file that cannot be opened is not reported,
        because refusing to submit over a permissions error would be worse than
        the tag it might not even contain.
        """
        found: List[str] = []
        for path in paths or []:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            if structure_relay.find_tags(text):
                found.append(path)
        return found

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
        self.txt_command = QLineEdit("orca {input} > {stem}.out")
        from .template_editor_dialog import PLACEHOLDER_TIP

        self.txt_command.setToolTip(PLACEHOLDER_TIP)
        self.txt_command.textChanged.connect(self._refresh_preview)
        self.cmb_template = QComboBox()
        self.cmb_template.setToolTip(
            "Conventional command line per program; picking one fills the Command field."
        )
        self.cmb_template.activated.connect(self._on_template_chosen)
        self._reload_templates()
        self.txt_globs = QLineEdit("*.out, *.log, *.xyz, *.hess, *.fchk")
        self.txt_globs.setToolTip("Which files come back when the job ends, comma separated.")
        #: What the last applied template put in the patterns field. An edit
        #: away from it means the user has an opinion, and the next template
        #: leaves the field alone.
        self._globs_before_template: list = []
        self.chk_auto_download = QCheckBox("Download results automatically when the job ends")
        self.chk_auto_download.setChecked(bool(self.store.get_pref("auto_download", True)))
        self.chk_auto_download.toggled.connect(self._on_auto_download_toggled)

        self.chk_download_all = QCheckBox("Download all output files")
        self.chk_download_all.setToolTip(
            "Fetch everything the job produced, ignoring the patterns above."
        )
        self.chk_download_all.setChecked(bool(self.store.get_pref("download_all_outputs", True)))
        self.chk_download_all.toggled.connect(
            lambda checked: self.store.set_pref("download_all_outputs", bool(checked))
        )
        self.chk_download_all.setEnabled(self.chk_auto_download.isChecked())

        self.chk_beside_input = QCheckBox("...next to the input file")
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
        # editingFinished, not textChanged: a preference is written to disk
        # with an fsync, and saving one per keystroke is a write per character
        # typed into this field.
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

        self.chk_chain = QCheckBox("Run after the job already queued on this host")
        self.chk_chain.setToolTip(
            "Hold this job until the one already queued on this host has finished."
        )
        self.chk_chain.setChecked(True)
        self.chk_chain_any = QCheckBox("...even if that job fails")
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
        """Fill the host list, opening on the one submitted to last time.

        Whichever host is alphabetically first is not a useful default: people
        submit to the same machine for weeks at a time, and picking the wrong
        one is not always obvious before pressing Submit. A prefilled host --
        from Resubmit, or from an input generator's handoff -- still wins, since
        that is applied after this.
        """
        self.cmb_host.blockSignals(True)
        self.cmb_host.clear()
        for host in self.store.host_list():
            self.cmb_host.addItem(f"{host.name} ({host.target})", host.id)
        remembered = self.cmb_host.findData(self.store.get_pref("last_host_id", "") or "")
        if remembered >= 0:
            self.cmb_host.setCurrentIndex(remembered)
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
        self._update_chain_row()
        # Relay candidates are host-scoped, so a host change re-filters them
        # even while the box is unchecked -- cheap, and it means the dropdown
        # is never a tick behind by the time it is opened.
        self._reload_relay_sources()

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
                    f"{scheduler.label if scheduler else 'This host'} has no queue to read it. "
                    "Cores and memory still matter."
                )

    #: How each scheduler is told to wait, for the hint under the checkbox.
    _CHAIN_MECHANISM: ClassVar[dict] = {
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

        if host is not None and host.uses_remote_runner:
            # There is a queue on that host already, and it schedules on cores
            # and memory. Chaining on top of it fixes an order it would have
            # worked out for itself, turns independent jobs into one line that
            # a single cancellation breaks, and stops anything else starting
            # while the job in front waits. So: off, and say why. It stays
            # available for a sequence that really does depend on the one
            # before it.
            self.chk_chain.setVisible(chainable)
            self.chk_chain.setEnabled(chainable and predecessor is not None)
            self.chk_chain.setChecked(False)
            self.chk_chain_any.setVisible(False)
            self.lbl_chain.setVisible(True)
            waiting = len(self.store.runnable_jobs(host.id))
            self.lbl_chain.setText(
                f"The queue on {host.name} decides when this starts: it runs what fits "
                f"in the cores and memory it has ({waiting} job(s) there now). "
                "Tick above only if this job needs the previous one to finish first."
            )
            return

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

    def add_files(self, paths: Sequence[str], batch: Optional[bool] = None) -> None:
        """Add input files from the picker or from a drop, and follow up.

        The follow-up is the point: naming the job, offering the right command
        template for the extension, and refreshing the preview are what make an
        added file useful rather than just listed.

        ``batch`` sets the "one job per file" checkbox where it is not None --
        used by a drop, which knows from the Shift key whether the files it
        carried should become separate jobs. The file picker leaves it alone:
        multi-selecting from a dialog is a deliberate act of building one job's
        input list, not the "I dropped a pile of calculations" case batch mode
        is for.
        """
        added = [p for p in paths or [] if p and p not in self.selected_files()]
        for path in added:
            self.list_files.addItem(path)
        if added:
            self.store.set_pref("last_input_dir", os.path.dirname(added[0]))
            if not self.txt_job_name.text().strip():
                self.txt_job_name.setText(os.path.splitext(os.path.basename(added[0]))[0])
            self._apply_scanned_resources(added[0])
        self._update_batch_row()
        self._update_relay_row()
        if batch is not None:
            self.chk_batch.setChecked(bool(batch))
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
        # Several files at once, dropped plainly, is a pile of separate
        # calculations far more often than it is one job's worth of inputs --
        # so that is the default, and Shift is held for the one job it used to
        # always mean. A single file is unaffected either way.
        batch = None
        if len(paths) > 1:
            batch = not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.add_files(paths, batch=batch)

    def _remove_file(self) -> None:
        for item in self.list_files.selectedItems():
            self.list_files.takeItem(self.list_files.row(item))
        self._update_batch_row()
        self._update_relay_row()
        self._refresh_preview()

    # --- preview ------------------------------------------------------------

    def _batch_active(self) -> bool:
        return self.chk_batch.isEnabled() and self.chk_batch.isChecked()

    def _update_batch_row(self) -> None:
        """Show the batch checkbox only where it means something.

        More than one file, and not "work already on the host": that box names
        one file already there for ``{input}``, and a batch has no single file
        that could answer.
        """
        files = self.selected_files()
        allowed = (
            len(files) > 1 and not self.box_remote.isChecked() and not self.box_relay.isChecked()
        )
        self.chk_batch.setVisible(len(files) > 1)
        self.chk_batch.setEnabled(allowed)
        if not allowed and self.chk_batch.isChecked():
            self.chk_batch.blockSignals(True)
            self.chk_batch.setChecked(False)
            self.chk_batch.blockSignals(False)
        self.btn_submit.setText(f"Submit {len(files)} Jobs" if self._batch_active() else "Submit")

    def _on_batch_toggled(self, checked: bool) -> None:
        if checked and self.chk_chain.isChecked():
            # A batch exists to run several calculations independently; a
            # chain would serialise it into the very thing batch mode is for
            # avoiding. Off by default, not forced -- a batch that really
            # should queue one after another is still one tick away.
            self.chk_chain.setChecked(False)
        self.btn_submit.setText(
            f"Submit {len(self.selected_files())} Jobs" if checked else "Submit"
        )
        self._update_relay_row()
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        host = self.current_host()
        if host is None:
            self.txt_preview.setPlainText("Add a host profile first (Hosts...).")
            return
        files = self.selected_files()
        if self._batch_active():
            self.lbl_preview_hint.setText(
                f"{len(files)} separate jobs will be submitted, one per file, each "
                "running this script. Shown below for the first file."
            )
        else:
            self.lbl_preview_hint.setText(
                "This exact script is uploaded and submitted. The trailing "
                "sentinel is how the plugin detects completion."
            )
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
        if self._batch_active() and files:
            # The name a batch job actually gets: its own file's stem, not
            # whatever happens to be in the Job name field.
            name = os.path.splitext(os.path.basename(files[0]))[0]
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
        batch = self._batch_active() and len(files) > 1
        if batch and remote_dir:
            # Guarded against in the UI by disabling one box while the other
            # is checked; asserted here too in case a caller drives the model
            # directly. "Work already on the host" names one file for
            # {input}, and a batch has no single file that could answer.
            QMessageBox.warning(
                self,
                "Submit",
                "Batch submission cannot be combined with a directory already on the host.",
            )
            return
        if not self._confirm_duplicate(files):
            return
        if not ensure_password(self.service, host, self):
            return
        # Above the batch branch, not beside the relay one: batch mode excludes
        # the relay box altogether, so every tagged file in a batch shipped
        # verbatim with nothing to substitute it. Unticked, the tag reaches the
        # host as a literal filename and the program reads "[prevfile:.xyz]" --
        # nothing failed until the calculation did, an hour later on the
        # cluster. This is also the path Resubmit takes, since it reopens the
        # wizard on the original file rather than on the substituted copy.
        if not self.box_relay.isChecked() and self._files_with_relay_tags(files):
            QMessageBox.warning(
                self,
                "Submit",
                "This input still contains a "
                f"{structure_relay.TAG_RE.pattern} tag, which only means "
                "something when a file is being reused from a previous job.\n\n"
                "Tick 'Reuse a file from a previous job' and choose that job, "
                "or edit the tag out of the input.",
            )
            return
        if batch:
            self._submit_batch(host, preset, files)
            self.accept()
            return
        relay_source_dir = ""
        relay_filenames: List[str] = []
        relay_job: Optional[Job] = None
        upload_files: Optional[List[str]] = None
        if self.box_relay.isChecked():
            # Original paths are what the duplicate check above just looked
            # at; the substituted copies -- same basenames, a resolved
            # filename in place of each tag -- are what actually get
            # uploaded, so this happens last.
            relay_job = self.selected_relay_job()
            if relay_job is None:
                QMessageBox.warning(self, "Submit", "Choose a job to reuse a file from.")
                return
            if not relay_job.remote_dir:
                QMessageBox.warning(
                    self, "Submit", f"'{relay_job.name}' has no remote directory recorded."
                )
                return
            try:
                for path in files:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        for filename in structure_relay.relay_plan(handle.read(), relay_job):
                            if filename not in relay_filenames:
                                relay_filenames.append(filename)
                # Uploaded instead of the originals, but the job still belongs
                # to the files the user picked: recording these scratch copies
                # as its input put the results beside *them*, in a temp folder.
                upload_files = [structure_relay.materialize(path, relay_job) for path in files]
            except (structure_relay.StructureRelayError, OSError) as exc:
                QMessageBox.warning(self, "Submit", str(exc))
                return
            relay_source_dir = relay_job.remote_dir
        name = self.txt_job_name.text().strip() or self._default_job_name(files, remote_dir)
        after = self.chain_predecessor() if self.chain_requested() else None
        chain_any = self.chain_any_requested()
        if after is None and relay_job is not None and relay_job.is_active:
            # The relay source has not finished yet; the copy embedded in the
            # script only produces a real file once it has, so this job must
            # not start before then. afterok, not afterany: a relay source
            # that fails leaves nothing worth copying.
            after = relay_job
            chain_any = False
        self.service.submit(
            host,
            preset,
            name,
            files,
            after_job=after,
            start_after=self.selected_start_time(),
            chain_any=chain_any,
            relay_source_dir=relay_source_dir,
            relay_filenames=relay_filenames,
            remote_dir=remote_dir,
            remote_input=self.remote_input(),
            upload_files=upload_files,
        )
        self._remember(host, preset)
        self.accept()

    def _submit_batch(self, host: HostProfile, preset: SubmitPreset, files: List[str]) -> None:
        """Submit each file as its own job, named after itself.

        Chaining, when the user has asked for it, is resolved fresh for every
        file: :meth:`chain_predecessor` reads the store, so a job this same
        loop just added becomes the predecessor for the next one -- which is
        what lets "batch" and "run one after another" compose with nothing
        special-cased here. Each submission's own message ("Submitted X as Y")
        is what reports progress; there is nothing else to summarise once this
        returns, since the wizard closes right after.
        """
        for path in files:
            name = os.path.splitext(os.path.basename(path))[0]
            after = self.chain_predecessor() if self.chain_requested() else None
            self.service.submit(
                host,
                preset,
                name,
                [path],
                after_job=after,
                start_after=self.selected_start_time(),
                chain_any=self.chain_any_requested(),
            )
        self._remember(host, preset)

    def _remember(self, host: HostProfile, preset: SubmitPreset) -> None:
        """Keep this submission's settings as the starting point for the next.

        Presets are the named, deliberate version of this; a person who submits
        the same kind of job every day should not have to name one to stop
        retyping the walltime, the modules and the fetch patterns each time.
        """
        remembered = dict(self.store.get_pref("last_preset", {}) or {})
        remembered[host.id] = preset.to_dict()
        self.store.set_pref("last_preset", remembered)
        # Which host, as well as what was asked of it: the next submission
        # opens on this one rather than on whichever sorts first.
        self.store.set_pref("last_host_id", host.id)

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
