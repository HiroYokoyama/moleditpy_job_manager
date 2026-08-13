"""Job monitor: the live table of tracked jobs.

A model/view table rather than a QTableWidget, so a poll result repaints the
affected rows instead of rebuilding every cell on a timer.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QVariant
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .credentials import ensure_password
from .models import (
    STATE_BLOCKED,
    STATE_DONE,
    STATE_FAILED,
    STATE_LOST,
    STATE_PENDING,
    STATE_QUEUED,
    STATE_RUNNING,
    Job,
)
from .service import JobService
from .store import (
    JOB_EXTENSION,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    RECOMMENDED_MIN_POLL_INTERVAL,
)

#: Job lists this window opens -- archived or not. .json covers the files
#: written before the extension existed.
JOB_LIST_EXTENSIONS = (JOB_EXTENSION, ".json")
JOB_LIST_FILTER = f"Job lists (*{JOB_EXTENSION} *.json);;All files (*)"

COLUMNS = ("Name", "Host", "Queue ID", "State", "After", "Elapsed", "Updated")

_STATE_COLORS = {
    STATE_RUNNING: "#2e7d32",
    STATE_PENDING: "#b58900",
    STATE_DONE: "#0a7d8c",
    STATE_FAILED: "#c62828",
    STATE_LOST: "#8e24aa",
    STATE_QUEUED: "#6c757d",
    STATE_BLOCKED: "#c62828",
}


def format_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_stamp(stamp: float) -> str:
    if not stamp:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(stamp))


class JobTableModel(QAbstractTableModel):
    """Read-only view of the store's job list."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self._rows: List[Job] = []
        #: When set, the table shows this fixed list instead of the live store.
        self._archived: Optional[List[Job]] = None
        self.reload()

    def show_archive(self, jobs: Optional[List[Job]]) -> None:
        """Display an archived list, or None to go back to the live store."""
        self._archived = jobs
        self.reload()

    def reload(self) -> None:
        self.beginResetModel()
        if self._archived is not None:
            self._rows = list(self._archived)
        else:
            self._rows = self.service.store.job_list()
        self.endResetModel()

    def _is_waiting(self, job: Job) -> bool:
        """True while a chained job is still waiting for its predecessor."""
        if not job.after_job_id or not job.is_active:
            return False
        predecessor = self.service.store.jobs.get(job.after_job_id)
        return predecessor is not None and predecessor.is_active

    def display_state(self, job: Job) -> str:
        """What the State column says, which is not always ``job.state``.

        A chained job the queue calls PENDING is either still waiting its turn
        or waiting for something that already failed, and those two deserve
        very different reactions from the user.
        """
        if self.service.store.chain_blocker(job) is not None:
            return STATE_BLOCKED
        if self._is_waiting(job):
            return STATE_QUEUED
        return job.state

    def predecessor_of(self, job: Job) -> Optional[Job]:
        if not job.after_job_id:
            return None
        return self.service.store.jobs.get(job.after_job_id)

    def job_at(self, row: int) -> Optional[Job]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_of(self, job_id: str) -> int:
        for index, job in enumerate(self._rows):
            if job.id == job_id:
                return index
        return -1

    def refresh_job(self, job_id: str) -> None:
        row = self.row_of(job_id)
        if row < 0:
            self.reload()
            return
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    # --- Qt model interface -------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return QVariant()
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(COLUMNS):
            return COLUMNS[section]
        return QVariant()

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return QVariant()
        job = self.job_at(index.row())
        if job is None:
            return QVariant()

        if role == Qt.ItemDataRole.DisplayRole:
            column = index.column()
            if column == 0:
                return job.name
            if column == 1:
                return job.host_name
            if column == 2:
                return job.remote_job_id or "-"
            if column == 3:
                state = self.display_state(job)
                suffix = ""
                if state == STATE_FAILED and job.rc is not None:
                    suffix = f" (rc={job.rc})"
                return f"{state}{suffix}"
            if column == 4:
                predecessor = self.predecessor_of(job)
                if predecessor is None:
                    return "-"
                return predecessor.name + ("" if job.chain_any else " (on success)")
            if column == 5:
                return format_duration(job.elapsed())
            if column == 6:
                return format_stamp(job.updated_at)
        elif role == Qt.ItemDataRole.ForegroundRole and index.column() == 3:
            color = _STATE_COLORS.get(self.display_state(job))
            if color:
                return QColor(color)
        elif role == Qt.ItemDataRole.ToolTipRole:
            lines = [f"Remote: {job.remote_dir or '-'}"]
            if job.local_dir:
                lines.append(f"Local: {job.local_dir}")
            blocker = self.service.store.chain_blocker(job)
            if blocker is not None:
                lines.append(
                    f"Will never start: it waits for {blocker.name} to succeed, "
                    f"and that job {blocker.state.lower()}."
                )
            if job.last_error:
                lines.append(f"Error: {job.last_error}")
            return "\n".join(lines)
        return QVariant()


class JobsDialog(QDialog):
    """The main Job Manager window."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Job Manager")
        self.resize(940, 560)
        #: Non-empty while a cleared list is being viewed read-only.
        self._archive_path = ""
        # Dropping a job list opens it: read-only when the file says it is
        # archived, otherwise it becomes the list in use for this session.
        self.setAcceptDrops(True)
        self.model = JobTableModel(service, self)
        self._build_ui()
        self._connect_service()
        self._update_buttons()
        self._update_interval_warning()

    # --- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.btn_new = QPushButton("New Job...")
        self.btn_new.clicked.connect(self.open_submit_dialog)
        self.btn_hosts = QPushButton("Hosts...")
        self.btn_hosts.clicked.connect(self.open_hosts_dialog)
        self.btn_refresh = QPushButton("Refresh Now")
        self.btn_refresh.clicked.connect(self._refresh_now)
        toolbar.addWidget(self.btn_new)
        toolbar.addWidget(self.btn_hosts)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("Poll every"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL)
        self.spin_interval.setSingleStep(30)
        self.spin_interval.setSuffix(" s")
        self.spin_interval.setValue(self.service.store.poll_interval)
        self.spin_interval.setToolTip(
            "One status query per host per cycle. Fast intervals are allowed, but a "
            f"shared login node is not a status API -- {RECOMMENDED_MIN_POLL_INTERVAL} s "
            "or slower is the courteous setting."
        )
        self.spin_interval.valueChanged.connect(self._on_interval_changed)
        toolbar.addWidget(self.spin_interval)
        self.lbl_interval_warning = QLabel("")
        self.lbl_interval_warning.setStyleSheet("color: #d08000;")
        toolbar.addWidget(self.lbl_interval_warning)
        layout.addLayout(toolbar)

        self.lbl_archive = QLabel("")
        self.lbl_archive.setWordWrap(True)
        self.lbl_archive.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.lbl_archive.setStyleSheet(
            "background: #fff3cd; color: #664d03; padding: 6px; border-radius: 4px;"
        )
        self.lbl_archive.setVisible(False)
        self.lbl_active_file = QLabel("")
        self.lbl_active_file.setWordWrap(True)
        self.lbl_active_file.setStyleSheet(
            "background: #e7f1ff; color: #084298; padding: 6px; border-radius: 4px;"
        )
        self.lbl_active_file.setVisible(False)
        active_row = QHBoxLayout()
        active_row.addWidget(self.lbl_active_file, 1)
        self.btn_default_file = QPushButton("Use the default list")
        self.btn_default_file.clicked.connect(self._use_default_job_list)
        self.btn_default_file.setVisible(False)
        active_row.addWidget(self.btn_default_file)
        layout.addLayout(active_row)

        archive_row = QHBoxLayout()
        archive_row.addWidget(self.lbl_archive, 1)
        self.btn_back = QPushButton("Back to current jobs")
        self.btn_back.clicked.connect(self._exit_archive)
        self.btn_back.setVisible(False)
        archive_row.addWidget(self.btn_back)
        layout.addLayout(archive_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        splitter.addWidget(self.table)

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText("Job output and messages appear here.")
        splitter.addWidget(self.txt_log)
        splitter.setSizes([380, 160])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancel Job")
        self.btn_cancel.clicked.connect(self._cancel_selected)
        self.btn_download = QPushButton("Download")
        self.btn_download.clicked.connect(self._download_selected)
        self.btn_open = QPushButton("Open Result")
        self.btn_open.clicked.connect(self._open_selected_result)
        self.btn_tail = QPushButton("Tail Log")
        self.btn_tail.clicked.connect(self._tail_selected)
        self.btn_resubmit = QPushButton("Resubmit")
        self.btn_resubmit.setToolTip(
            "Open the submit wizard prefilled from this job: same host, same "
            "resources, same input files."
        )
        self.btn_resubmit.clicked.connect(self._resubmit_selected)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_save_as = QPushButton("Save As...")
        self.btn_save_as.setToolTip(
            f"Save the job list to a {JOB_EXTENSION} file: the same records the "
            "plugin stores, and openable again from here or File > Import."
        )
        self.btn_save_as.clicked.connect(lambda: self._export(JOB_EXTENSION))
        self.btn_export_csv = QPushButton("Export CSV")
        self.btn_export_csv.setToolTip("Write one row per job: state, exit code, timings, paths.")
        self.btn_export_csv.clicked.connect(lambda: self._export(".csv"))
        self.btn_archive = QPushButton("Load Archive...")
        self.btn_archive.setToolTip("View a previously cleared job list, read only.")
        self.btn_archive.clicked.connect(self._load_archive)
        self.btn_clear = QPushButton("Clear List...")
        self.btn_clear.setToolTip(
            "Empty the table. The current list is saved to the archived folder "
            "first, with the date in its name -- nothing is deleted on the cluster."
        )
        self.btn_clear.clicked.connect(self._clear_jobs)
        for button in (
            self.btn_cancel,
            self.btn_download,
            self.btn_open,
            self.btn_tail,
            self.btn_resubmit,
            self.btn_remove,
        ):
            actions.addWidget(button)
        actions.addSpacing(16)
        for button in (
            self.btn_save_as,
            self.btn_export_csv,
            self.btn_archive,
            self.btn_clear,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        self.chk_auto_open = QCheckBox("Open results automatically")
        self.chk_auto_open.setChecked(
            bool(self.service.store.get_pref("open_result_after_download", True))
        )
        self.chk_auto_open.toggled.connect(
            lambda checked: self.service.store.set_pref("open_result_after_download", checked)
        )
        actions.addWidget(self.chk_auto_open)

        self.chk_taskbar_badge = QCheckBox("Show the count on the app icon")
        self.chk_taskbar_badge.setToolTip(
            "Put the number of active jobs on MoleditPy's icon in the task bar "
            "(the Dock on macOS, the launcher entry on Linux).\n\n"
            "Off by default: the application icon belongs to MoleditPy, not to "
            "this plugin. The status bar counter is shown either way."
        )
        self.chk_taskbar_badge.setChecked(bool(self.service.store.get_pref("taskbar_badge", False)))
        self.chk_taskbar_badge.toggled.connect(self._on_taskbar_badge_toggled)
        actions.addWidget(self.chk_taskbar_badge)
        layout.addLayout(actions)

        self.lbl_status = QLabel("")
        layout.addWidget(self.lbl_status)

    def _connect_service(self) -> None:
        # Kept as a list so closeEvent can undo every one of them. The service
        # outlives this window, so a connection left behind is a closed dialog
        # that still reloads its model on every poll and still opens each
        # finished job's results -- once per window the user ever opened.
        self._connections = [
            (self.service.jobs_changed, self.model.reload),
            (self.service.jobs_changed, self._update_buttons),
            (self.service.job_updated, self.model.refresh_job),
            (self.service.job_updated, self._on_job_updated),
            (self.service.message, self._append_message),
            (self.service.error, self._append_error),
            (self.service.log_ready, self._show_log),
            (self.service.results_ready, self._on_results_ready),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)

    def _disconnect_service(self) -> None:
        for signal, slot in getattr(self, "_connections", []):
            try:
                signal.disconnect(slot)
            except TypeError:
                logging.debug("Job Manager: signal already disconnected")
        self._connections = []

    def _on_job_updated(self, _job_id: str = "") -> None:
        self._update_buttons()

    def _on_taskbar_badge_toggled(self, enabled: bool) -> None:
        self.service.store.set_pref("taskbar_badge", bool(enabled))
        # Applied now rather than at the next poll: switching it off has to
        # take the badge off the icon, not leave the last count sitting there.
        from .taskbar import clear_badge

        if not enabled:
            clear_badge()
        self.service.jobs_changed.emit()

    # --- helpers ------------------------------------------------------------

    def viewing_archive(self) -> bool:
        return bool(self._archive_path)

    def selected_job(self) -> Optional[Job]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self.model.job_at(rows[0].row())

    def _append_message(self, text: str) -> None:
        self.txt_log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {text}")
        self.lbl_status.setText(text)

    def _append_error(self, text: str) -> None:
        self._append_message(text)

    def _show_log(self, text: str) -> None:
        self.txt_log.setPlainText(text)

    def _update_buttons(self) -> None:
        if self.viewing_archive():
            # An archived job's queue id is stale and its remote directory may
            # be long gone, so every action that would act on one is off.
            for button in (
                self.btn_cancel,
                self.btn_download,
                self.btn_open,
                self.btn_tail,
                self.btn_resubmit,
                self.btn_remove,
                self.btn_save_as,
                self.btn_export_csv,
                self.btn_clear,
            ):
                button.setEnabled(False)
            return

        for button in (self.btn_save_as, self.btn_export_csv, self.btn_clear):
            button.setEnabled(True)
        job = self.selected_job()
        has_job = job is not None
        self.btn_cancel.setEnabled(bool(job and job.is_active))
        self.btn_download.setEnabled(bool(job and job.remote_dir))
        self.btn_open.setEnabled(bool(job and job.downloaded_files))
        self.btn_tail.setEnabled(bool(job and job.remote_dir))
        self.btn_resubmit.setEnabled(bool(job and job.input_files))
        self.btn_remove.setEnabled(has_job)

    # --- actions ------------------------------------------------------------

    def open_submit_dialog(
        self,
        files: Optional[List[str]] = None,
        name: str = "",
        host_id: str = "",
        preset: Optional[dict] = None,
    ) -> None:
        from .submit_dialog import SubmitDialog

        if not self.service.store.hosts:
            QMessageBox.information(self, "Job Manager", "Add a host profile first (Hosts...).")
            self.open_hosts_dialog()
            if not self.service.store.hosts:
                return
        dialog = SubmitDialog(self.service, self)
        if files or name or host_id or preset:
            dialog.prefill(files=files, name=name, host_id=host_id, preset=preset)
        dialog.exec()

    def _resubmit_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        missing = [path for path in job.input_files if not os.path.isfile(path)]
        if missing:
            QMessageBox.warning(
                self, "Resubmit", f"The original input is no longer on disk:\n{missing[0]}"
            )
            return
        if job.host_id not in self.service.store.hosts:
            # Prefill cannot select a host that no longer exists, so the wizard
            # would silently open on whichever host happens to be first.
            confirm = QMessageBox.question(
                self,
                "Resubmit",
                f"The host profile '{job.host_name}' no longer exists.\n"
                "Resubmit against a different host?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self.open_submit_dialog(
            files=list(job.input_files),
            name=job.name,
            host_id=job.host_id,
            preset=job.preset or None,
        )

    def open_hosts_dialog(self) -> None:
        from .hosts_dialog import HostsDialog

        dialog = HostsDialog(self.service, self)
        dialog.exec()

    def _on_interval_changed(self, value: int) -> None:
        self.service.store.set_pref("poll_interval", int(value))
        self.service.poller.reschedule()
        self._update_interval_warning()

    def _update_interval_warning(self) -> None:
        """Fast polling is permitted, but never silent."""
        if not self.service.store.poll_interval_is_aggressive:
            self.lbl_interval_warning.setText("")
            self.lbl_interval_warning.setToolTip("")
            return
        self.lbl_interval_warning.setText("⚠ fast")
        self.lbl_interval_warning.setToolTip(
            f"Polling faster than {RECOMMENDED_MIN_POLL_INTERVAL} s hits the login node "
            "with a queue query every few seconds, for every host you have jobs on. "
            "Fine against your own machine or while debugging; on a shared cluster it "
            "is the kind of thing admins complain about."
        )

    def _refresh_now(self) -> None:
        if not self.service.poller.refresh_now():
            self._append_message("Refresh is rate limited; try again in a few seconds.")

    def _cancel_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        confirm = QMessageBox.question(
            self, "Cancel job", f"Cancel '{job.name}' ({job.remote_job_id}) on the cluster?"
        )
        if confirm == QMessageBox.StandardButton.Yes and self._has_credentials(job):
            self.service.cancel(job)

    def _has_credentials(self, job: Job) -> bool:
        """Prompt for this job's host password before any worker is dispatched."""
        host = self.service.store.hosts.get(job.host_id)
        if host is None:
            return True  # the service reports the missing profile itself
        return ensure_password(self.service, host, self)

    def _download_selected(self) -> None:
        job = self.selected_job()
        if job is not None and self._has_credentials(job):
            self.service.download(job)

    def _tail_selected(self) -> None:
        job = self.selected_job()
        if job is not None and self._has_credentials(job):
            self._append_message(f"Reading {job.log_file}...")
            self.service.tail(job)

    def _remove_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        confirm = QMessageBox.question(
            self,
            "Remove job",
            f"Remove '{job.name}' from the list?\nNothing is deleted on the cluster or on disk.",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.service.remove_job(job.id)

    def _export(self, extension: str) -> None:
        """Write the whole list out as raw JSON or as CSV."""
        store = self.service.store
        if not store.jobs:
            self._append_message("Nothing to export: the job list is empty.")
            return
        label = "CSV" if extension == ".csv" else "job list"
        default = os.path.join(
            store.download_root(), f"moleditpy_jobs_{time.strftime('%Y%m%d')}{extension}"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Save the {label}",
            default,
            f"{label.title()} (*{extension});;All files (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += extension
        try:
            store.export_jobs(path)
        except OSError as exc:
            QMessageBox.warning(self, "Export", f"Could not write {path}:\n{exc}")
            return
        self._append_message(f"Exported {len(store.jobs)} job(s) to {path}")

    def _load_archive(self) -> None:
        """Show a previously cleared list, read only."""
        store = self.service.store
        directory = store.archive_dir()
        if not os.path.isdir(directory):
            QMessageBox.information(
                self,
                "Load archive",
                f"There are no archives yet. Clearing the job list writes one here:\n{directory}",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open an archived job list", directory, JOB_LIST_FILTER
        )
        if path:
            self.open_job_list(path)

    def open_job_list(self, path: str) -> bool:
        """Open a job list: read-only if the file says it is archived.

        The flag travels in the file, so a cleared list stays history after it
        is moved, copied or mailed on. Anything not flagged -- an export, a
        backup, a colleague's file -- becomes the list in use for this session.
        """
        store = self.service.store
        jobs, archived = store.read_job_list(path)
        if not jobs:
            QMessageBox.warning(self, "Open job list", f"No jobs could be read from:\n{path}")
            return False
        if archived:
            return self._show_archive(path, jobs)
        return self._use_job_list(path, jobs)

    def _use_job_list(self, path: str, jobs: List[Job]) -> bool:
        """Switch the live table to this file for the rest of the session."""
        store = self.service.store
        confirm = QMessageBox.question(
            self,
            "Open job list",
            f"Use {os.path.basename(path)} ({len(jobs)} jobs) as the current job list?\n\n"
            "Tracking, polling and every later change go to this file until "
            "MoleditPy is restarted. Your usual list is not modified.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return False
        # Leave the read-only view first, or the table goes on showing the
        # archive -- with every button disabled -- while tracking and saving
        # have already moved to the file just opened.
        self._exit_archive()
        count = store.use_jobs_file(path)
        self.service.jobs_changed.emit()
        self.service.poller.start()
        self._update_active_file()
        self._append_message(f"Now using {path} ({count} jobs)")
        return True

    def _use_default_job_list(self) -> None:
        """Back to the usual job list."""
        store = self.service.store
        self._exit_archive()
        store.use_jobs_file("")
        self.service.jobs_changed.emit()
        self.service.poller.start()
        self._update_active_file()
        self._append_message("Back to the default job list")

    def _update_active_file(self) -> None:
        """Say which list is in use whenever it is not the usual one."""
        store = self.service.store
        if store.using_default_jobs_file():
            self.setWindowTitle("Job Manager")
            self.lbl_active_file.setVisible(False)
            self.btn_default_file.setVisible(False)
            return
        self.setWindowTitle(f"Job Manager — {os.path.basename(store.jobs_path)}")
        self.lbl_active_file.setText(
            f"Working in <b>{store.jobs_path}</b> for this session. "
            "Restarting comes back to the usual list."
        )
        self.lbl_active_file.setVisible(True)
        self.btn_default_file.setVisible(True)

    def _show_archive(self, path: str, jobs: List[Job]) -> bool:
        """Display an archived list read-only."""
        store = self.service.store
        directory = store.archive_dir()
        self._archive_path = path
        self.model.show_archive(jobs)
        self.lbl_archive.setText(
            f"Viewing <b>{os.path.basename(path)}</b> ({len(jobs)} jobs) — this list is "
            "marked archived, so it is read only. To delete archives permanently, "
            f"open {directory}"
        )
        self.lbl_archive.setVisible(True)
        self.btn_back.setVisible(True)
        self._update_buttons()
        self._append_message(f"Viewing {os.path.basename(path)} (read only)")
        return True

    def _exit_archive(self) -> None:
        """Back to the live job list."""
        self._archive_path = ""
        self.model.show_archive(None)
        self.lbl_archive.setVisible(False)
        self.btn_back.setVisible(False)
        self._update_buttons()

    def _clear_jobs(self) -> None:
        """Empty the table, keeping a dated copy of what was in it."""
        store = self.service.store
        if not store.jobs:
            return
        active = len(store.active_jobs())
        warning = (
            f"\n\n{active} of them are still active: clearing stops tracking them, "
            "but does not cancel anything on the cluster."
            if active
            else ""
        )
        confirm = QMessageBox.question(
            self,
            "Clear job list",
            f"Remove all {len(store.jobs)} job(s) from the list?\n"
            f"The current list is saved to {store.archive_dir()} first.{warning}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        archived, count = store.clear_jobs()
        self.service.jobs_changed.emit()
        self._append_message(f"Cleared {count} job(s); archived to {archived}")

    def _open_selected_result(self) -> None:
        job = self.selected_job()
        if job is None or not job.downloaded_files:
            return
        self.open_result_files(job.downloaded_files)

    def _on_results_ready(self, job_id: str, paths: list) -> None:
        if not self.chk_auto_open.isChecked():
            return
        self.open_result_files(paths)

    def open_result_files(self, paths: List[str]) -> None:
        """Hand the most interesting downloaded file to the host application."""
        target = pick_primary_result(paths)
        if not target:
            return
        opened = open_in_host(target)
        if opened:
            self._append_message(f"Opened {os.path.basename(target)}")
        else:
            self._append_message(f"Downloaded {target}")

    # --- drag and drop ------------------------------------------------------

    def _dropped_job_list(self, event) -> str:
        """The path of a single dropped job list, or "" if that is not what it is."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return ""
        urls = [url for url in mime.urls() if url.isLocalFile()]
        if len(urls) != 1:
            return ""
        path = urls[0].toLocalFile()
        if not path.lower().endswith(JOB_LIST_EXTENSIONS):
            return ""
        # Normalised like the input-file path beside it: toLocalFile() hands
        # back forward slashes on Windows, and two drop handlers on one window
        # returning different spellings of the same path is a trap.
        return os.path.normpath(path)

    @staticmethod
    def _dropped_input_files(event) -> List[str]:
        """Local files that are not a job list, i.e. things to submit.

        Dropping onto this window is unambiguous -- it is the job window --
        which is why input extensions are not registered with the host
        instead. Claiming ``.inp`` or ``.xyz`` application-wide would take
        those files away from being *opened*, which is what a user dropping one
        on the main window usually means.
        """
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        return [
            os.path.normpath(path)
            for path in paths
            if path and os.path.isfile(path) and not path.lower().endswith(JOB_LIST_EXTENSIONS)
        ]

    def dragEnterEvent(self, event) -> None:
        if self._dropped_job_list(event) or self._dropped_input_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event) -> None:
        path = self._dropped_job_list(event)
        if path:
            event.acceptProposedAction()
            self.open_job_list(path)
            return
        # Anything else that is a real file is treated as something to run:
        # the wizard opens prefilled, so a drop is the whole way from "here is
        # my input" to "which cluster, which command".
        files = self._dropped_input_files(event)
        if not files:
            event.ignore()
            return
        event.acceptProposedAction()
        self.open_submit_dialog(files=files)

    # --- lifecycle ----------------------------------------------------------

    def _teardown(self) -> None:
        """Let go of the service. Safe to call twice.

        Deregisters too, so a reopened window is a fresh, live instance;
        polling continues in the service, which outlives this dialog.
        """
        self._disconnect_service()
        try:
            from . import forget_window

            forget_window()
        except Exception:
            logging.debug("Job Manager: window deregistration failed", exc_info=True)

    def reject(self) -> None:
        # Esc closes a QDialog through reject(), which never reaches
        # closeEvent. Without this the window stayed connected to a service
        # that outlives it: every poll reloaded a dead dialog's model, and each
        # finished job opened its results once per window ever dismissed.
        self._teardown()
        super().reject()

    def closeEvent(self, event) -> None:
        self._teardown()
        # Accepted rather than delegated: QDialog's own closeEvent calls
        # reject(), and reject() now tears down as well -- doing both would
        # recurse.
        event.accept()


#: Extensions an analyzer plugin is most likely to claim, best first.
_RESULT_PRIORITY = (".out", ".log", ".fchk", ".hess", ".xyz")


def pick_primary_result(paths: List[str]) -> str:
    for extension in _RESULT_PRIORITY:
        for path in paths or []:
            if path.lower().endswith(extension):
                return path
    return (paths or [""])[0]


def open_in_host(path: str) -> bool:
    """Route a downloaded file through the application's own file openers.

    Reuses ``MainWindow.init_manager.load_command_line_file``, which walks the
    registered plugin file openers by priority (that is how the ORCA Result
    Analyzer claims ``.out``) before falling back to the built-in loaders --
    so no analyzer plugin needs to be hard-coded here.
    """
    from . import get_context

    context = get_context()
    if context is None or not path or not os.path.exists(path):
        return False
    try:
        main_window = context.get_main_window()
    except Exception:
        logging.debug("Job Manager: no main window available", exc_info=True)
        return False

    init_manager = getattr(main_window, "init_manager", None)
    loader = getattr(init_manager, "load_command_line_file", None)
    if callable(loader):
        try:
            loader(path)
            return True
        except Exception:
            logging.warning("Job Manager: host could not open %s", path, exc_info=True)
            return False

    # Older hosts: dispatch to the highest-priority plugin opener directly.
    plugin_manager = getattr(main_window, "plugin_manager", None)
    openers = getattr(plugin_manager, "file_openers", {}) or {}
    extension = os.path.splitext(path)[1].lower()
    for opener in openers.get(extension, []):
        callback = opener.get("callback") if isinstance(opener, dict) else None
        if not callable(callback):
            continue
        try:
            callback(path)
            return True
        except Exception:
            logging.warning("Job Manager: opener failed for %s", path, exc_info=True)
    return False
