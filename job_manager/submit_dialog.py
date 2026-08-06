"""New Job dialog: pick a host, an input file and a submit preset."""

from __future__ import annotations

import os
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .command_templates import CommandTemplate, suggest, templates_for
from .credentials import ensure_password
from .models import HostProfile, SubmitPreset
from .schedulers import get_scheduler
from .service import JobService

INPUT_FILTER = "Calculation inputs (*.inp *.com *.gjf *.in *.xyz *.sh *.slurm);;All files (*)"

#: Dropdown entries that are actions rather than templates.
_SAVE_TEMPLATE = object()
_DELETE_TEMPLATE = object()


class SubmitDialog(QDialog):
    """Collects everything needed for one submission and previews the script."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.store = service.store
        self.setWindowTitle("Job Manager - Submit Job")
        self.resize(760, 640)
        self._build_ui()
        self._reload_hosts()

    # --- prefilling ---------------------------------------------------------

    def prefill(
        self,
        files: Optional[List[str]] = None,
        name: str = "",
        host_id: str = "",
        preset: Optional[dict] = None,
    ) -> None:
        """Populate the form from outside.

        Used by the input-generator handoff (a file that was just written) and
        by Resubmit (a previous job's host, preset and inputs).
        """
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
        if name:
            self.txt_job_name.setText(name)
        elif files:
            self.txt_job_name.setText(os.path.splitext(os.path.basename(files[0]))[0])
        self._reload_templates()
        self._apply_suggested_template()
        self._refresh_preview()

    # --- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

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

        files_box = QGroupBox("Input files (the first one is passed to the command)")
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

        tabs = QTabWidget()
        tabs.addTab(self._build_resources_tab(), "Resources")
        tabs.addTab(self._build_preview_tab(), "Script preview")
        layout.addWidget(tabs, 1)

        preset_row = QHBoxLayout()
        save_preset = QPushButton("Save as preset")
        save_preset.clicked.connect(self._save_preset)
        preset_row.addWidget(save_preset)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Submit")
        box.accepted.connect(self._submit)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

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
        self.txt_command.textChanged.connect(self._refresh_preview)
        self.cmb_template = QComboBox()
        self.cmb_template.setToolTip(
            "Conventional command line for each program MoleditPy writes input "
            "for. Picking one fills the Command field, which stays editable."
        )
        self.cmb_template.activated.connect(self._on_template_chosen)
        self._reload_templates()
        self.txt_globs = QLineEdit("*.out, *.log, *.xyz, *.hess, *.fchk")
        self.chk_auto_download = QCheckBox("Download results automatically when the job ends")
        self.chk_auto_download.setChecked(True)

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
        form.addRow("Modules", self.txt_modules)
        form.addRow("Pre-commands", self.txt_pre)
        form.addRow("Extra directives", self.txt_extra)
        command_row = QWidget()
        command_layout = QHBoxLayout(command_row)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.addWidget(self.txt_command, 1)
        command_layout.addWidget(self.cmb_template)
        form.addRow("Command", command_row)
        form.addRow("Fetch patterns", self.txt_globs)
        form.addRow("", self.chk_auto_download)
        return page

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
        self.chk_auto_download.setChecked(bool(preset.auto_download))

    def collect_preset(self) -> SubmitPreset:
        host = self.current_host()
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
            fetch_globs=[g.strip() for g in self.txt_globs.text().split(",") if g.strip()],
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
                    entry["label"], CommandTemplate(entry["label"], entry["command"])
                )
                self.cmb_template.setItemData(
                    self.cmb_template.count() - 1,
                    entry["command"],
                    Qt.ItemDataRole.ToolTipRole,
                )

        self.cmb_template.insertSeparator(self.cmb_template.count())
        self.cmb_template.addItem("Save current command as...", _SAVE_TEMPLATE)
        if saved:
            self.cmb_template.addItem("Delete a saved template...", _DELETE_TEMPLATE)
        self.cmb_template.blockSignals(False)

    def _on_template_chosen(self, index: int) -> None:
        choice = self.cmb_template.itemData(index)
        self.cmb_template.setCurrentIndex(0)
        if choice is _SAVE_TEMPLATE:
            self._save_user_template()
        elif choice is _DELETE_TEMPLATE:
            self._delete_user_template()
        elif choice is not None:
            self.txt_command.setText(choice.command)

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
        self.store.add_user_template(label.strip(), command)
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

    def _apply_suggested_template(self) -> None:
        """Fill an empty command from the input's extension; never overwrite."""
        files = self.selected_files()
        if not files or self.txt_command.text().strip():
            return
        template = suggest(os.path.basename(files[0]))
        if template is not None and template.command:
            self.txt_command.setText(template.command)

    # --- files --------------------------------------------------------------

    def selected_files(self) -> List[str]:
        return [self.list_files.item(row).text() for row in range(self.list_files.count())]

    def _add_files(self) -> None:
        start = self.store.get_pref("last_input_dir", "") or ""
        paths, _ = QFileDialog.getOpenFileNames(self, "Select input files", start, INPUT_FILTER)
        for path in paths or []:
            if path and path not in self.selected_files():
                self.list_files.addItem(path)
        if paths:
            self.store.set_pref("last_input_dir", os.path.dirname(paths[0]))
            if not self.txt_job_name.text().strip():
                self.txt_job_name.setText(os.path.splitext(os.path.basename(paths[0]))[0])
        self._reload_templates()
        self._apply_suggested_template()
        self._refresh_preview()

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
        input_name = os.path.basename(files[0]) if files else "input.inp"
        try:
            scheduler = get_scheduler(host.scheduler)
        except ValueError as exc:
            self.txt_preview.setPlainText(str(exc))
            return
        script = scheduler.build_script(
            self.txt_job_name.text().strip() or "moleditpy_job",
            self.collect_preset(),
            input_name,
            "job.log",
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
        if not files:
            QMessageBox.warning(self, "Submit", "Select at least one input file.")
            return
        missing = [path for path in files if not os.path.isfile(path)]
        if missing:
            QMessageBox.warning(self, "Submit", f"File not found:\n{missing[0]}")
            return
        preset = self.collect_preset()
        if not preset.command_template.strip():
            QMessageBox.warning(self, "Submit", "Enter the command to run.")
            return
        if not ensure_password(self.service, host, self):
            return
        name = self.txt_job_name.text().strip() or os.path.basename(files[0])
        self.service.submit(host, preset, name, files)
        self.accept()
