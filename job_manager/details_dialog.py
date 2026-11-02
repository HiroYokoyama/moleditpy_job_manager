"""Everything recorded about one job, with the few parts still worth changing.

Most of a job record is history: the host it went to, the script that ran, the
exit code it came back with. Those are shown and not touched -- editing them
would describe a job that never existed.

Four things are not history, because they decide what happens *next*: the name,
whether results are fetched when it ends, which files count as results, and
where they land. All four are routinely wrong exactly once -- after a job has
been submitted and before it has finished -- which is the one moment the old
read-only view offered nothing but retyping the submission.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .file_table import FilePathTable
from .models import MAX_PRIORITY, MIN_PRIORITY, Job, hold_reasons
from .theme import apply_theme
from .window_utils import make_independent


class JobDetailsDialog(QDialog):
    """The job record, plus the settings that still apply to its results."""

    def __init__(
        self,
        service,
        job: Job,
        record: str,
        title: str,
        parent: Optional[QWidget] = None,
        *,
        read_only: bool = False,
        describe=None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.job = job
        self.read_only = read_only
        self._describe = describe
        self._queue_busy = False
        self._priority_dirty = False
        self._updates_connected = True
        self.setWindowTitle(title)
        make_independent(self)
        apply_theme(self)
        self.resize(820, 620)

        layout = QVBoxLayout(self)

        box = QGroupBox("Settings that still apply")
        form = QFormLayout(box)
        self.txt_name = QLineEdit(job.name)
        self.txt_name.setToolTip("What this job is called in the table and in notifications.")
        self.txt_globs = QLineEdit(", ".join(job.fetch_globs or []))
        self.txt_globs.setToolTip(
            "Which files are fetched from the job directory when it ends, comma separated."
        )
        self.chk_auto = QCheckBox("Download when the job ends")
        self.chk_auto.setChecked(bool(job.auto_download))
        self.chk_auto.setToolTip(
            "Fetch the results as soon as the job finishes. Unticked, they stay "
            "on the host until you press Download."
        )

        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.txt_local = QLineEdit(job.local_dir)
        self.txt_local.setPlaceholderText("decided when downloading (beside the input)")
        self.txt_local.setToolTip(
            "Where the results are written. Empty means it is chosen at download time."
        )
        browse = QPushButton("...")
        browse.setMaximumWidth(32)
        browse.clicked.connect(self._browse)
        folder_layout.addWidget(self.txt_local)
        folder_layout.addWidget(browse)

        form.addRow("Name", self.txt_name)
        form.addRow("Fetch patterns", self.txt_globs)
        form.addRow("Results folder", folder_row)
        form.addRow("", self.chk_auto)
        layout.addWidget(box)

        scheduling = QGroupBox("Queue settings — applied immediately")
        queue_form = QFormLayout(scheduling)
        self.lbl_hold = QLabel()
        self.lbl_hold.setWordWrap(True)
        queue_form.addRow("Hold", self.lbl_hold)
        priority_row = QHBoxLayout()
        self.spin_priority = QSpinBox()
        self.spin_priority.setRange(MIN_PRIORITY, MAX_PRIORITY)
        self.spin_priority.setValue(job.priority)
        self.spin_priority.valueChanged.connect(self._priority_edited)
        self.btn_priority = QPushButton("Apply Priority")
        self.btn_priority.clicked.connect(self._apply_priority)
        priority_row.addWidget(self.spin_priority)
        priority_row.addWidget(self.btn_priority)
        self.btn_hold = QPushButton("Hold")
        self.btn_hold.clicked.connect(lambda: self._apply_hold(True))
        self.btn_release = QPushButton("Release")
        self.btn_release.setToolTip("Remove the manual hold. After conditions remain in effect.")
        self.btn_release.clicked.connect(lambda: self._apply_hold(False))
        priority_row.addWidget(self.btn_hold)
        priority_row.addWidget(self.btn_release)
        priority_row.addStretch()
        queue_form.addRow("Priority", priority_row)
        layout.addWidget(scheduling)

        tabs = QTabWidget()
        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        for caption, path in (("Remote folder", job.remote_dir), ("Results folder", job.local_dir)):
            row = QHBoxLayout()
            row.addWidget(QLabel(caption))
            field = QLineEdit(path)
            field.setReadOnly(True)
            field.setCursorPosition(0)
            field.setToolTip(path)
            if caption == "Results folder":
                self.results_folder_view = field
            row.addWidget(field, 1)
            files_layout.addLayout(row)
        self.file_table = FilePathTable(self)
        self.file_table.setAccessibleName("Recorded job files")
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels(["File", "Folder", "Status", "Role"])
        for role, paths in (("Input", job.input_files), ("Downloaded", job.downloaded_files)):
            for path in paths:
                self.file_table.add_path(path)
                self.file_table.setItem(self.file_table.rowCount() - 1, 3, QTableWidgetItem(role))
        files_layout.addWidget(self.file_table, 1)
        self.txt_file_path = QLineEdit()
        self.txt_file_path.setReadOnly(True)
        self.txt_file_path.setPlaceholderText(
            "Select a recorded file to read or copy its full path"
        )
        files_layout.addWidget(self.txt_file_path)
        self.file_table.itemSelectionChanged.connect(self._show_file_path)
        tabs.addTab(files_page, "Files and folders")
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setPlainText(record)
        tabs.addTab(self.view, "Full record and script")
        layout.addWidget(tabs, 1)

        self.lbl_saved = QLabel("")
        self.lbl_saved.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.lbl_saved)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self._save)
        # Only through rejected: a Close button already emits it, and wiring
        # its clicked as well called reject() twice, so finished fired twice.
        buttons.rejected.connect(self.reject)
        if read_only:
            buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            for widget in (self.txt_name, self.txt_globs, self.txt_local, self.chk_auto):
                widget.setEnabled(False)
        layout.addWidget(buttons)
        self.service.job_updated.connect(self._refresh_queue)
        self.service.jobs_changed.connect(self._refresh_queue)
        self.finished.connect(self._disconnect_updates)
        self._refresh_queue()

    def _disconnect_updates(self, *_args) -> None:
        if not self._updates_connected:
            return
        self._updates_connected = False
        self.service.job_updated.disconnect(self._refresh_queue)
        self.service.jobs_changed.disconnect(self._refresh_queue)

    def _priority_edited(self) -> None:
        self._priority_dirty = True

    def _refresh_queue(self, *_args) -> None:
        current = self.service.store.jobs.get(self.job.id) is self.job
        editable = current and not self.read_only and not self._queue_busy
        reasons = hold_reasons(self.job, self.service.store.jobs)
        self.lbl_hold.setText(" / ".join(reasons) or "None")
        can_priority = editable and not self.service.priority_refusal(self.job)
        self.spin_priority.setEnabled(bool(can_priority))
        self.btn_priority.setEnabled(bool(can_priority))
        self.btn_hold.setEnabled(bool(editable and not self.service.hold_refusal(self.job, True)))
        self.btn_release.setEnabled(
            bool(editable and not self.service.hold_refusal(self.job, False))
        )
        if not self._priority_dirty:
            self.spin_priority.blockSignals(True)
            self.spin_priority.setValue(self.job.priority)
            self.spin_priority.blockSignals(False)
        if self._describe is not None:
            record = self._describe(self.job)
            if record != self.view.toPlainText():
                cursor = self.view.textCursor()
                position, anchor = cursor.position(), cursor.anchor()
                scroll = self.view.verticalScrollBar().value()
                self.view.setPlainText(record)
                cursor = self.view.textCursor()
                end = self.view.document().characterCount() - 1
                cursor.setPosition(min(anchor, end))
                cursor.setPosition(min(position, end), cursor.MoveMode.KeepAnchor)
                self.view.setTextCursor(cursor)
                self.view.verticalScrollBar().setValue(scroll)

    def _queue_operation(self, operation, *, priority: bool = False) -> None:
        if (
            self.read_only
            or self._queue_busy
            or self.service.store.jobs.get(self.job.id) is not self.job
        ):
            return
        from .credentials import ensure_password

        host = self.service.store.hosts.get(self.job.host_id)
        if host is None or not ensure_password(self.service, host, self):
            return
        self._queue_busy = True
        self._refresh_queue()

        def done(job, updated):
            self._queue_busy = False
            if updated and priority:
                self._priority_dirty = False
            self.lbl_saved.setText(
                "Queue setting updated."
                if updated
                else "The job is no longer waiting in the queue."
            )
            self._refresh_queue()

        def failed(message):
            self._queue_busy = False
            self.lbl_saved.setText(message)
            self._refresh_queue()

        operation(on_done=done, on_error=failed, owner=self)

    def _apply_priority(self) -> None:
        value = self.spin_priority.value()
        self._queue_operation(
            lambda **callbacks: self.service.set_priority(self.job, value, **callbacks),
            priority=True,
        )

    def _apply_hold(self, held: bool) -> None:
        self._queue_operation(
            lambda **callbacks: self.service.set_hold(self.job, held, **callbacks)
        )

    def _show_file_path(self) -> None:
        rows = self.file_table.selected_rows()
        paths = self.file_table.paths()
        self.txt_file_path.setText(paths[rows[0]] if rows else "")
        self.txt_file_path.setCursorPosition(0)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Results folder", self.txt_local.text())
        if path:
            self.txt_local.setText(path)

    def _save(self) -> None:
        """Apply the four fields and persist, then let the table catch up."""
        if self.read_only:
            return
        if self.service.store.jobs.get(self.job.id) is not self.job:
            self.lbl_saved.setText("This job list has changed. Reopen the details to edit it.")
            return
        self.job.name = self.txt_name.text().strip() or self.job.name
        self.job.fetch_globs = [
            pattern.strip() for pattern in self.txt_globs.text().split(",") if pattern.strip()
        ]
        self.job.local_dir = self.txt_local.text().strip()
        self.job.auto_download = bool(self.chk_auto.isChecked())
        self.results_folder_view.setText(self.job.local_dir)
        self.results_folder_view.setToolTip(self.job.local_dir)
        self.results_folder_view.setCursorPosition(0)
        self.service.store.save_jobs()
        self.service.jobs_changed.emit()
        # Said plainly rather than in the title bar, which is where the job's
        # own name lives and should stay.
        self.lbl_saved.setText("Saved. The patterns and folder apply to the next download.")


__all__ = ["JobDetailsDialog"]
