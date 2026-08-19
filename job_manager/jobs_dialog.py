"""Job monitor: the live table of tracked jobs.

A model/view table rather than a QTableWidget, so a poll result repaints the
affected rows instead of rebuilding every cell on a timer.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, List, Optional


from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QVariant,
)
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)


from . import PLUGIN_VERSION, webhook
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
from .theme import (
    CY_AMBER,
    CY_GREEN,
    CY_GREY,
    CY_PURPLE,
    CY_RED,
    CY_TEAL,
    apply_theme,
)
from .tasks import run_async
from .window_utils import make_independent
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

#: Used for the two banners above the table. Palette roles rather than fixed
#: pastels: the old pair were light-theme colours, so a dark-theme user read
#: navy on near-white inside an otherwise dark window.  The left border picks
#: up the accent colour so the banner stands out without a hard background.
from .theme import CY_ACCENT2 as _ACCENT2  # noqa: E402 – after other imports

BANNER_STYLE = (
    f"background: palette(alternate-base); color: palette(text); "
    f"border: 1px solid palette(mid); border-left: 3px solid {_ACCENT2}; "
    "padding: 6px 10px; border-radius: 4px;"
)


COLUMNS = ("Name", "Host", "Queue ID", "State", "After", "Elapsed", "Updated")

_STATE_COLORS = {
    STATE_RUNNING: CY_GREEN,
    STATE_PENDING: CY_AMBER,
    STATE_DONE: CY_TEAL,
    STATE_FAILED: CY_RED,
    STATE_LOST: CY_PURPLE,
    STATE_QUEUED: CY_GREY,
    STATE_BLOCKED: CY_RED,
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


class _StateColorDelegate(QStyledItemDelegate):
    """Keeps the State column's colour when its row is selected.

    Qt's item delegate paints selected text with the palette's HighlightedText
    role and ignores the model's ForegroundRole entirely while a row is
    selected -- so RUNNING/FAILED/etc. all rendered as the same near-black
    text the moment you clicked the row, on top of a blue highlight that made
    it worse. Overriding the palette colours the delegate paints with, rather
    than the pen colour after the fact, is what actually takes effect for both
    the selected and unselected states.
    """

    def initStyleOption(self, option, index) -> None:  # noqa: N802 - Qt's spelling
        super().initStyleOption(option, index)
        color = index.data(Qt.ItemDataRole.ForegroundRole)
        if color is not None:
            option.palette.setColor(option.palette.ColorRole.Text, color)
            option.palette.setColor(option.palette.ColorRole.HighlightedText, color)


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
        # The cached set, not chain_blocker: this is asked twice per visible
        # row per repaint -- once for the text, once for the colour -- and each
        # call walked the job's whole chain through the scheduler registry.
        if job.id in self.service.store.blocked_ids():
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
                # Running time once it is running or over; queue wait before.
                if job.is_terminal or job.state == STATE_RUNNING or job.started_at:
                    return format_duration(job.elapsed())
                return f"wait {format_duration(job.waiting())}"
            if column == 6:
                return format_stamp(job.updated_at)
        elif role == Qt.ItemDataRole.UserRole:
            # What the column is actually sorted on: the raw value behind the
            # formatted text, so "10m" does not sort before "2m" and the
            # newest job is not decided by string order on its timestamp.
            column = index.column()
            if column == 5:
                return (
                    job.elapsed()
                    if (job.is_terminal or job.state == STATE_RUNNING or job.started_at)
                    else job.waiting()
                )
            if column == 6:
                return job.updated_at
            return self.data(index, Qt.ItemDataRole.DisplayRole)
        elif role == Qt.ItemDataRole.ForegroundRole and index.column() == 3:
            color = _STATE_COLORS.get(self.display_state(job))
            if color:
                return QColor(color)
        elif role == Qt.ItemDataRole.ToolTipRole:
            lines = [f"Remote: {job.remote_dir or '-'}"]
            if job.local_dir:
                lines.append(f"Local: {job.local_dir}")
            if job.submitted_at:
                lines.append(f"Queue wait: {format_duration(job.waiting())}")
            if job.started_at or job.is_terminal:
                lines.append(f"Run time: {format_duration(job.elapsed())}")
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


class JobFilterProxyModel(QSortFilterProxyModel):
    """Sits between the table and :class:`JobTableModel`: click a header to
    sort, type to filter -- without either changing what the model itself
    holds.

    Sorting reads UserRole, not the formatted text: Elapsed is shown as
    "10m 05s", and a plain text sort would put it before "2m 05s". Filtering
    matches any column, not only the first, because a search for a host name
    or a queue id is exactly as reasonable as one for a job name.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._search = ""
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_search_text(self, text: str) -> None:
        text = (text or "").strip().lower()
        if text == self._search:
            return
        self._search = text
        self.invalidateFilter()

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        left_value = self.sourceModel().data(left, Qt.ItemDataRole.UserRole)
        right_value = self.sourceModel().data(right, Qt.ItemDataRole.UserRole)
        if left_value is None or right_value is None:
            return super().lessThan(left, right)
        try:
            return left_value < right_value
        except TypeError:
            # A mismatched pair (a QVariant() on one side, a real value on the
            # other) is rare but not impossible mid-reload; text is always
            # comparable, and a merely-approximate order there is no loss.
            return str(left_value) < str(right_value)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._search:
            return True
        model = self.sourceModel()
        for column in range(model.columnCount()):
            value = model.index(source_row, column, source_parent).data(Qt.ItemDataRole.DisplayRole)
            if value and self._search in str(value).lower():
                return True
        return False


class JobsDialog(QDialog):
    """The main Job Manager window."""

    def __init__(self, service: JobService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        # The version is in the title of every window: a bug report that names
        # it is worth several rounds of asking.
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Job Monitor")
        make_independent(self)
        apply_theme(self)
        self.resize(940, 560)
        #: Non-empty while a cleared list is being viewed read-only.
        self._archive_path = ""
        #: The open log window, if any; the tail goes there rather than into
        #: the four-line strip at the bottom.
        self._tail_dialog: Optional[QDialog] = None
        #: The live host panel, if open. One is enough, and it polls.
        self._host_monitor: Optional[QDialog] = None
        #: Detail windows stay open until closed, so they have to be held.
        self._detail_dialogs: List[QDialog] = []
        # Dropping a job list opens it: read-only when the file says it is
        # archived, otherwise it becomes the list in use for this session.
        self.setAcceptDrops(True)
        self.model = JobTableModel(service, self)
        self._build_ui()
        self._connect_service()
        self._update_buttons()
        self._update_interval_warning()
        # Elapsed is computed from the clock every time the cell is drawn, but
        # a cell is only drawn when the model says it changed -- which happened
        # on a poll result, so the column advanced in two-minute jumps. This
        # repaints one column, reads nothing, and contacts nobody.
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick_elapsed)
        self._ticker.start()

    # --- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.btn_new = QPushButton("New Job...")
        self.btn_new.clicked.connect(self.open_submit_dialog)
        self.btn_hosts = QPushButton("Hosts...")
        self.btn_hosts.clicked.connect(self.open_hosts_dialog)
        self.btn_refresh = QPushButton("Refresh Now")
        self.btn_refresh.clicked.connect(self._refresh_now)
        self.btn_host_monitor = QPushButton("Host Monitor...")
        self.btn_host_monitor.setToolTip(
            "Live load and memory per host, sampled only while that window is open."
        )
        self.btn_host_monitor.clicked.connect(self.open_host_monitor)
        toolbar.addWidget(self.btn_new)
        toolbar.addWidget(self.btn_hosts)
        toolbar.addWidget(self.btn_refresh)
        toolbar.addWidget(self.btn_host_monitor)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("Poll every"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL)
        self.spin_interval.setSingleStep(30)
        self.spin_interval.setSuffix(" s")
        self.spin_interval.setValue(self.service.store.poll_interval)
        self.spin_interval.setToolTip(
            "One status query per host per cycle. "
            f"{RECOMMENDED_MIN_POLL_INTERVAL} s or slower is the courteous setting."
        )
        self.spin_interval.valueChanged.connect(self._on_interval_changed)
        toolbar.addWidget(self.spin_interval)
        self.lbl_interval_warning = QLabel("")
        self.lbl_interval_warning.setStyleSheet(f"color: {CY_AMBER};")
        toolbar.addWidget(self.lbl_interval_warning)
        layout.addLayout(toolbar)

        self.lbl_archive = QLabel("")
        self.lbl_archive.setWordWrap(True)
        self.lbl_archive.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.lbl_archive.setStyleSheet(BANNER_STYLE)
        self.lbl_archive.setVisible(False)
        self.lbl_active_file = QLabel("")
        self.lbl_active_file.setWordWrap(True)
        self.lbl_active_file.setStyleSheet(BANNER_STYLE)
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

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter"))
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("Filter jobs by name, host, queue id or state...")
        self.txt_filter.setClearButtonEnabled(True)
        self.txt_filter.textChanged.connect(self._apply_job_filter)
        filter_row.addWidget(self.txt_filter, 1)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableView()
        # A proxy between the table and the model, not the model itself:
        # JobTableModel stays the plain, testable read-only view of the store
        # it always was, and every existing row-index caller (job_at, row_of,
        # the elapsed ticker) keeps addressing it directly by source row.
        self.proxy = JobFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicator(6, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setItemDelegateForColumn(3, _StateColorDelegate(self.table))
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        self.table.doubleClicked.connect(lambda *_: self._open_double_clicked())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_row_menu)
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
        self.btn_open.setToolTip("Open one of this job's output files in MoleditPy.")
        self.btn_open.clicked.connect(self._open_selected_result)
        self.btn_tail = QPushButton("Tail Log")
        self.btn_tail.setToolTip("Read the end of the job's log in a window of its own.")
        self.btn_tail.clicked.connect(self._tail_selected)
        self.btn_tail_file = QPushButton("Tail File...")
        self.btn_tail_file.setToolTip(
            "Read the tail of a chosen remote output/log file in the job's directory."
        )
        self.btn_tail_file.clicked.connect(self._tail_specific_file)
        self.btn_details = QPushButton("Details")
        self.btn_details.setToolTip("Everything recorded about this job, and the script that ran.")
        self.btn_details.clicked.connect(self._show_details)
        self.btn_resubmit = QPushButton("Resubmit")
        self.btn_resubmit.setToolTip(
            "Open the submit wizard prefilled from this job: same host, same "
            "resources, same input files."
        )
        self.btn_resubmit.clicked.connect(self._resubmit_selected)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_selected)
        # The counterparts to Save As..., at the head of the row that is about
        # the list rather than about one job. Opening a list was reachable only
        # through Load Archive... or a banner that appears once you are already
        # somewhere else.
        self.btn_open_default = QPushButton("Default")
        self.btn_open_default.setToolTip(
            "Back to the job list this plugin keeps in ~/.moleditpy/job_manager/."
        )
        self.btn_open_default.clicked.connect(self._use_default_job_list)
        self.btn_open_list = QPushButton("Open...")
        self.btn_open_list.setToolTip(
            f"Open a saved job list ({JOB_EXTENSION}). A cleared list opens read only."
        )
        self.btn_open_list.clicked.connect(self._open_job_list_file)
        self.btn_save_as = QPushButton("Save As...")
        self.btn_save_as.setToolTip(
            f"Save the job list to a {JOB_EXTENSION} file, openable again from here."
        )
        self.btn_save_as.clicked.connect(lambda: self._export(JOB_EXTENSION))
        self.btn_export_csv = QPushButton("Export CSV")
        self.btn_export_csv.setToolTip("Write one row per job: state, exit code, timings, paths.")
        self.btn_export_csv.clicked.connect(lambda: self._export(".csv"))
        self.btn_rebuild = QPushButton("Rebuild from Folder...")
        self.btn_rebuild.setToolTip(
            "Build a job list from results already on disk. The list is read "
            "only: nothing in it can be submitted or polled."
        )
        self.btn_rebuild.clicked.connect(self._rebuild_from_folder)
        self.btn_archive = QPushButton("Load Archive...")
        self.btn_archive.setToolTip("View a previously cleared job list, read only.")
        self.btn_archive.clicked.connect(self._load_archive)
        self.btn_clear = QPushButton("Clear List...")
        self.btn_clear.setToolTip(
            "Empty the table, saving a dated copy first. Nothing on the host is deleted."
        )
        self.btn_clear.clicked.connect(self._clear_jobs)
        # Two rows, split by what they act on: the selected job above, the list
        # as a whole below. Ten buttons on one line ran off the side of a narrow
        # window, and put "Clear List..." within a slip of "Cancel Job".
        for button in (
            self.btn_cancel,
            self.btn_download,
            self.btn_open,
            self.btn_tail,
            self.btn_tail_file,
            self.btn_details,
            self.btn_resubmit,
            self.btn_remove,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        list_actions = QHBoxLayout()
        for button in (
            self.btn_open_default,
            self.btn_open_list,
            self.btn_save_as,
            self.btn_export_csv,
            self.btn_rebuild,
            self.btn_archive,
            self.btn_clear,
        ):
            list_actions.addWidget(button)
        list_actions.addStretch(1)
        layout.addLayout(list_actions)

        # And the preferences on a line of their own: they are not actions.
        actions = QHBoxLayout()
        self.chk_auto_open = QCheckBox("Open results automatically")
        self.chk_auto_open.setChecked(
            bool(self.service.store.get_pref("open_result_after_download", True))
        )
        self.chk_auto_open.toggled.connect(
            lambda checked: self.service.store.set_pref("open_result_after_download", checked)
        )
        actions.addWidget(self.chk_auto_open)

        self.chk_taskbar_badge = QCheckBox("Show the count on the app icon")
        self.chk_taskbar_badge.setToolTip("Show the number of active jobs on MoleditPy's own icon.")
        self.chk_taskbar_badge.setChecked(bool(self.service.store.get_pref("taskbar_badge", False)))
        self.chk_taskbar_badge.toggled.connect(self._on_taskbar_badge_toggled)
        actions.addWidget(self.chk_taskbar_badge)

        self.chk_notify = QCheckBox("Notify me when a job ends")
        self.chk_notify.setToolTip("Raise a desktop notification when a tracked job ends.")
        self.chk_notify.setChecked(bool(self.service.store.get_pref("notify_on_finish", True)))
        self.chk_notify.toggled.connect(
            lambda checked: self.service.store.set_pref("notify_on_finish", bool(checked))
        )
        actions.addWidget(self.chk_notify)

        self.chk_chat = QCheckBox("Post to chat")
        self.chk_chat.toggled.connect(
            lambda checked: self.service.store.set_pref("notify_chat", bool(checked))
        )
        actions.addWidget(self.chk_chat)

        self.btn_chat = QPushButton("Chat alerts...")
        self.btn_chat.setToolTip(
            "Also post to Slack, Discord or Teams when a job ends, so the news "
            "reaches you away from this machine."
        )
        self.btn_chat.clicked.connect(self._edit_chat_webhook)
        actions.addWidget(self.btn_chat)
        self._sync_chat_controls()
        actions.addStretch(1)
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

    def _edit_chat_webhook(self) -> None:
        from .chat_webhook_dialog import ChatWebhookDialog

        dialog = ChatWebhookDialog(self.service.store, self, pool=self.service.pool)
        dialog.exec()
        self._sync_chat_controls()

    def _sync_chat_controls(self) -> None:
        """Show the tick as what it is: unusable until a room is configured.

        A tick that can be set with no webhook behind it says messages are
        being posted while nothing is, which is the worst of the three states
        to be in -- it is believed, and only a job ending disproves it.
        """
        url = str(self.service.store.get_pref("notify_webhook", "") or "")
        # Blocked: this runs whenever the URL might have changed, and letting
        # setChecked through here would write the *displayed* state back over
        # the user's own setting every time the dialog was merely opened.
        self.chk_chat.blockSignals(True)
        self.chk_chat.setChecked(bool(url) and bool(self.service.store.get_pref("notify_chat")))
        self.chk_chat.blockSignals(False)
        self.chk_chat.setEnabled(bool(url))
        self.chk_chat.setToolTip(
            f"Post to {webhook.service_name(url)} as well, when a job ends."
            if url
            else "Set a webhook URL under Chat alerts... first."
        )

    # --- helpers ------------------------------------------------------------

    def viewing_archive(self) -> bool:
        return bool(self._archive_path)

    def _apply_job_filter(self, text: str) -> None:
        self.proxy.set_search_text(text)

    def selected_job(self) -> Optional[Job]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        # The view's model is the sort/filter proxy, so a selected row's index
        # is a proxy row and has to be mapped back to the source model that
        # actually holds the job.
        source = self.proxy.mapToSource(rows[0])
        return self.model.job_at(source.row())

    def _append_message(self, text: str) -> None:
        self.txt_log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {text}")
        self.lbl_status.setText(text)

    def _append_error(self, text: str) -> None:
        self._append_message(text)

    def _show_log(self, text: str) -> None:
        if self._tail_dialog is not None:
            self._tail_dialog.set_text(text)
            return
        # No window open: the tail was asked for by something else, or the
        # window was closed while the read was in flight.
        self.txt_log.setPlainText(text)

    def _show_row_menu(self, position) -> None:
        """Everything that can be done to the row under the cursor.

        The same actions as the buttons, and disabled on the same conditions --
        driven from the buttons themselves so the two can never disagree about
        what is possible for a job.
        """
        index = self.table.indexAt(position)
        if index.isValid():
            self.table.selectRow(index.row())
        self._update_buttons()
        if self.selected_job() is None:
            return

        menu = QMenu(self)
        for button in (
            self.btn_tail,
            self.btn_tail_file,
            self.btn_details,
            self.btn_open,
            self.btn_download,
            self.btn_resubmit,
            self.btn_cancel,
            self.btn_remove,
        ):
            if button is self.btn_resubmit or button is self.btn_cancel:
                menu.addSeparator()
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.setToolTip(button.toolTip())
            action.triggered.connect(button.click)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _tick_elapsed(self) -> None:
        """Repaint the Elapsed cell of every job that is still going."""
        if not self.isVisible():
            # A hidden window paints nothing; waking Qt up for it is waste.
            return
        column = COLUMNS.index("Elapsed")
        for row in range(self.model.rowCount()):
            job = self.model.job_at(row)
            if job is not None and job.is_active:
                index = self.model.index(row, column)
                self.model.dataChanged.emit(index, index)

    def viewing_reconstructed(self) -> bool:
        """True while the list in use was rebuilt from a folder."""
        return bool(getattr(self.service.store, "reconstructed", False))

    def _update_buttons(self) -> None:
        if self.viewing_reconstructed() and not self.viewing_archive():
            # Everything that would talk to a host is off: these jobs were read
            # off a disk and there is no host, no queue id and no remote
            # directory behind any of them. Opening a result, reading the
            # record and exporting the list all still work, and are the whole
            # point of having rebuilt it.
            job = self.selected_job()
            for button in (
                self.btn_new,
                self.btn_cancel,
                self.btn_download,
                self.btn_tail,
                self.btn_tail_file,
                self.btn_resubmit,
            ):
                button.setEnabled(False)
            self.btn_open.setEnabled(bool(job and job.downloaded_files))
            self.btn_details.setEnabled(job is not None)
            self.btn_remove.setEnabled(job is not None)
            for button in (self.btn_save_as, self.btn_export_csv, self.btn_clear):
                button.setEnabled(True)
            return
        self.btn_new.setEnabled(True)
        if self.viewing_archive():
            # An archived job's queue id is stale and its remote directory may
            # be long gone, so every action that would act on one is off.
            for button in (
                self.btn_cancel,
                self.btn_download,
                self.btn_open,
                self.btn_tail,
                self.btn_tail_file,
                self.btn_resubmit,
                self.btn_remove,
                self.btn_save_as,
                self.btn_export_csv,
                self.btn_clear,
            ):
                button.setEnabled(False)
            self.btn_details.setEnabled(self.selected_job() is not None)
            return

        for button in (self.btn_save_as, self.btn_export_csv, self.btn_clear):
            button.setEnabled(True)
        job = self.selected_job()
        has_job = job is not None
        self.btn_cancel.setEnabled(bool(job and job.is_active))
        self.btn_download.setEnabled(bool(job and job.remote_dir))
        mirror_ready = False
        if job and job.remote_dir:
            host = self.service.store.hosts.get(job.host_id)
            if host is not None:
                mirror_dir = host.mirrored_job_dir(job.remote_dir)
                mirror_ready = bool(mirror_dir and os.path.isdir(mirror_dir))
        self.btn_open.setEnabled(
            bool(
                job
                and (job.downloaded_files or (job.downloaded and job.remote_dir) or mirror_ready)
            )
        )
        self.btn_tail.setEnabled(bool(job and job.remote_dir))
        self.btn_tail_file.setEnabled(bool(job and job.remote_dir))
        # A command-only job has no input files to check for; what makes it
        # resubmittable is the command, which the preset snapshot carries.
        self.btn_resubmit.setEnabled(bool(job and (job.input_files or job.preset)))
        self.btn_remove.setEnabled(has_job)
        # Details reads only what is already recorded, so it needs no host and
        # works for an archived job too -- which is when it is most useful.
        self.btn_details.setEnabled(has_job)

    # --- actions ------------------------------------------------------------

    def open_submit_dialog(
        self,
        files: Optional[List[str]] = None,
        name: str = "",
        host_id: str = "",
        preset: Optional[dict] = None,
        remote_dir: str = "",
        remote_input: str = "",
        batch: bool = False,
        handoff: bool = False,
    ) -> None:
        from .submit_dialog import SubmitDialog

        if self.viewing_reconstructed():
            # Including a drop onto the window, which lands here as well: a
            # rebuilt list has nowhere to submit to and nothing to track with.
            QMessageBox.information(
                self,
                "Job Manager",
                "This job list was rebuilt from a folder, so it is read only.\n\n"
                "Press Default to go back to your own list before submitting.",
            )
            return
        if not self.service.store.hosts:
            QMessageBox.information(self, "Job Manager", "Add a host profile first (Hosts...).")
            self.open_hosts_dialog()
            if not self.service.store.hosts:
                return
        dialog = SubmitDialog(self.service, self)
        if files or name or host_id or preset or remote_dir or handoff:
            dialog.prefill(
                files=files,
                name=name,
                host_id=host_id,
                preset=preset,
                remote_dir=remote_dir,
                remote_input=remote_input,
                batch=batch,
                handoff=handoff,
            )
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
            # A job that ran on work already staged on the host is resubmitted
            # against that same directory: there is no local input to send.
            remote_dir=job.remote_dir if job.remote_dir_provided else "",
            remote_input=job.remote_input,
        )

    def open_host_monitor(self) -> None:
        """Open the live host panel, or raise the one already up."""
        from .host_monitor import HostMonitorDialog

        if self._host_monitor is not None:
            self._host_monitor.show()
            self._host_monitor.raise_()
            self._host_monitor.activateWindow()
            return
        dialog = HostMonitorDialog(self.service, parent=None)
        self._host_monitor = dialog
        dialog.finished.connect(lambda *_: setattr(self, "_host_monitor", None))
        dialog.show()

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
        self.lbl_interval_warning.setText("fast polling")
        self.lbl_interval_warning.setToolTip(
            f"Faster than {RECOMMENDED_MIN_POLL_INTERVAL} s queries the login node every "
            "few seconds, for every host you have jobs on."
        )

    def _refresh_now(self) -> None:
        if not self.service.poller.refresh_now():
            self._append_message("Refresh is rate limited; try again in a few seconds.")

    def _cancel_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        dependents = [
            j for j in self.service.store.dependents_of(job.id, recursive=True) if j.is_active
        ]
        if not dependents:
            confirm = QMessageBox.question(
                self, "Cancel job", f"Cancel '{job.name}' ({job.remote_job_id}) on the host?"
            )
            if confirm == QMessageBox.StandardButton.Yes and self._has_credentials(job):
                self.service.cancel(job)
            return
        # A chain is the case where "cancel" is ambiguous: one job, or that job
        # and everything queued behind it. Asked outright rather than decided
        # here, because both answers are ones people really want.
        box = QMessageBox(self)
        box.setWindowTitle("Cancel job")
        box.setText(
            f"'{job.name}' has {len(dependents)} job(s) queued behind it.\n\n"
            "Cancel this one and let the rest run, or cancel the whole chain?"
        )
        this_one = box.addButton("Cancel this job", QMessageBox.ButtonRole.AcceptRole)
        whole_chain = box.addButton("Cancel the chain", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (this_one, whole_chain) or not self._has_credentials(job):
            return
        if clicked is whole_chain:
            # Behind first, so nothing is released by the cancel of the job in
            # front of it and started while the chain is being taken down.
            for dependent in reversed(dependents):
                self.service.cancel(dependent, release_dependents=False)
            self.service.cancel(job, release_dependents=False)
            return
        self.service.cancel(job)

    def _has_credentials(self, job: Job) -> bool:
        """Prompt for this job's host password before any worker is dispatched."""
        host = self.service.store.hosts.get(job.host_id)
        if host is None:
            return True  # the service reports the missing profile itself
        return ensure_password(self.service, host, self)

    def _download_selected(self) -> None:
        """Show what is on the host and let the user pick. Nothing is fetched
        until they say so.

        The automatic download follows the fetch patterns and says nothing when
        they match nothing, which is the usual way a finished job appears to
        have produced no results. Pressing the button is a deliberate act, so
        it asks: which files, and into which folder.
        """
        job = self.selected_job()
        if job is None or not self._has_credentials(job):
            return
        self.btn_download.setEnabled(False)
        self._append_message(f"Listing {job.remote_dir}...")

        def listed(names: list) -> None:
            self.btn_download.setEnabled(True)
            self._offer_download(job, names)

        def failed(message: str) -> None:
            self.btn_download.setEnabled(True)
            self._append_message(message)

        self.service.list_remote_results(job, listed, failed)

    def _offer_download(self, job: Job, names: list) -> None:
        from .download_dialog import DownloadDialog
        from .runner import is_plugin_file, likely_outputs, select_files

        matched = [
            name
            for name in select_files(names, job.fetch_globs or [])
            # Offered, never pre-ticked: `*.log` is in the default patterns for
            # Gaussian's output, and ticking the wrapper's log on its account
            # would be the automatic download this is meant to replace.
            if not is_plugin_file(name, job.log_file)
        ]
        # Nothing matched is the case this dialog exists for, and a list with
        # nothing ticked leaves the user to find their own output among the
        # scratch files. The names say which those are.
        suggested = not matched
        if suggested:
            matched = likely_outputs(names, job.log_file)
        dialog = DownloadDialog(
            job.name,
            names,
            matched,
            job.local_dir or self.service.store.download_root(),
            f"Job Manager {PLUGIN_VERSION} - Download {job.name}",
            self,
            suggested=suggested,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        folder = dialog.folder()
        if not chosen or not folder:
            return
        self.service.download(job, into=folder, names=chosen)

    def _open_double_clicked(self) -> None:
        """What a double click means depends on whether the job is still going.

        While it runs, the question is "how far has it got", and the answer is
        the log. Once it has finished, the log is the least interesting file in
        the directory -- what was wanted is the result -- and opening it there
        meant every finished job took a second click to get past.
        """
        job = self.selected_job()
        if job is None:
            return
        if job.is_terminal and self.btn_open.isEnabled():
            self._open_selected_result()
            return
        if self.btn_tail.isEnabled():
            self._tail_selected()

    def _tail_selected(self) -> None:
        job = self.selected_job()
        if job is None or not self._has_credentials(job):
            return
        from .models import BACKEND_OPENSSH
        from .text_dialog import TextDialog

        host = self.service.store.hosts.get(job.host_id)
        auto_interval = 10 if (host and host.backend == BACKEND_OPENSSH) else 5

        if self._tail_dialog is None:
            self._tail_dialog = TextDialog(
                f"Job Manager {PLUGIN_VERSION} - {job.name}: {job.log_file}",
                "Reading...",
                self,
                on_refresh=lambda: self.service.tail(job),
                auto_interval=auto_interval,
                store=self.service.store,
            )

            # Cleared on close so the next tail builds a live window rather
            # than writing into a destroyed one.
            self._tail_dialog.finished.connect(lambda *_: setattr(self, "_tail_dialog", None))
            self._tail_dialog.show()
        else:
            # Update the window title AND the refresh callback so the
            # auto-refresh pulls from the newly selected job, not the old one.
            self._tail_dialog.setWindowTitle(
                f"Job Manager {PLUGIN_VERSION} - {job.name}: {job.log_file}"
            )
            self._tail_dialog._on_refresh_callback = lambda: self.service.tail(job)
            self._tail_dialog.raise_()
            self._tail_dialog.activateWindow()
        self.service.tail(job)

    def _tail_specific_file(self) -> None:
        job = self.selected_job()
        if job is None or not self._has_credentials(job):
            return

        self._append_message(f"Listing remote files for {job.name}...")

        def on_files_listed(names: list) -> None:
            if not self.isVisible():
                return
            filtered = [n for n in names if n and not n.startswith(".")]
            if not filtered:
                filtered = [job.log_file or "job.log"]

            from .runner import primary_output
            from .tail_file_dialog import TailFileDialog

            # The calculation's own output, never the wrapper's log. This
            # window exists for the file the program writes; the wrapper's log
            # is what the Tail Log button already opens, and preselecting it
            # here meant the one file this dialog is not for was the one
            # offered. It stays in the list -- it is a real file on the host
            # and asking for it deliberately is allowed -- just not first.
            dialog = TailFileDialog(
                job.name,
                filtered,
                default_file=primary_output(filtered, job.log_file or ""),
                log_file=job.log_file or "",
                title=f"Job Manager {PLUGIN_VERSION} - Tail Specific File: {job.name}",
                parent=self,
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                chosen = dialog.chosen()
                if chosen:
                    self._open_tail_for_file(job, chosen)

        def on_list_error(msg: str) -> None:
            if not self.isVisible():
                return
            chosen, ok = QInputDialog.getText(
                self,
                "Tail Specific File",
                f"Enter filename to tail in {job.remote_dir}:",
                text=job.log_file or "",
            )
            if ok and chosen.strip():
                self._open_tail_for_file(job, chosen.strip())

        self.service.list_remote_results(job, on_files_listed, on_list_error)

    def _open_tail_for_file(self, job: Job, filename: str) -> None:
        from .models import BACKEND_OPENSSH
        from .text_dialog import TextDialog

        host = self.service.store.hosts.get(job.host_id)
        auto_interval = 10 if (host and host.backend == BACKEND_OPENSSH) else 5

        dialog = TextDialog(
            f"Job Manager {PLUGIN_VERSION} - {job.name}: {filename}",
            "Reading...",
            self,
            on_refresh=lambda: self._refresh_tail_file(job, filename, dialog),
            auto_interval=auto_interval,
            store=self.service.store,
        )
        self._detail_dialogs.append(dialog)
        dialog.finished.connect(
            lambda *_: self._detail_dialogs.remove(dialog)
            if dialog in self._detail_dialogs
            else None
        )
        dialog.show()
        self._refresh_tail_file(job, filename, dialog)

    def _refresh_tail_file(self, job: Job, filename: str, dialog: Any) -> None:
        def on_done(text: str) -> None:
            try:
                dialog.set_text(text)
            except RuntimeError:
                pass

        def on_err(msg: str) -> None:
            try:
                dialog.set_text(f"Could not tail {filename}: {msg}")
            except RuntimeError:
                pass

        self.service.tail_file(job, filename, on_done=on_done, on_error=on_err)

    def _show_details(self) -> None:
        """Everything recorded about this job, including the script that ran."""
        job = self.selected_job()
        if job is None:
            return

        from .details_dialog import JobDetailsDialog

        dialog = JobDetailsDialog(
            self.service,
            job,
            self._describe(job),
            f"Job Manager {PLUGIN_VERSION} - {job.name}",
            self,
        )
        dialog.show()
        # Held so Python does not collect the window the moment this returns.
        self._detail_dialogs.append(dialog)
        # Discarded rather than removed: finished can arrive more than once for
        # one window, and the second remove() raised ValueError out of a Qt
        # slot -- which the host reports to the user as a plugin crash.
        dialog.finished.connect(lambda *_: self._forget_detail(dialog))

    def _forget_detail(self, dialog) -> None:
        """Drop a closed details window, however many times we are told."""
        if dialog in self._detail_dialogs:
            self._detail_dialogs.remove(dialog)

    def _describe(self, job: Job) -> str:
        """The job record as text: what was asked for, and what happened."""
        host = self.service.store.hosts.get(job.host_id)
        rows = [
            ("Name", job.name),
            ("State", job.state + (f" (exit {job.rc})" if job.rc is not None else "")),
            ("Host", job.host_name or (host.name if host else "(profile removed)")),
            ("Scheduler", job.scheduler),
            ("Queue id", job.remote_job_id or "-"),
            ("Submitted", format_stamp(job.submitted_at)),
            ("Started", format_stamp(job.started_at)),
            ("Finished", format_stamp(job.finished_at)),
            ("Remote directory", job.remote_dir),
            ("Log file", job.log_file),
            ("Input files", ", ".join(job.input_files) or "-"),
            ("Downloaded to", job.local_dir or "-"),
            ("Last error", job.last_error or "-"),
        ]
        # The resources it was submitted with, from the snapshot taken at
        # submit time rather than the named preset -- which may since have been
        # edited or deleted, and would then describe a different job.
        preset = job.preset or {}
        if preset:
            rows += [
                ("", ""),
                ("Command", preset.get("command_template", "")),
                ("Queue / partition", preset.get("queue", "") or "-"),
                ("Account", preset.get("account", "") or "-"),
                ("Walltime", preset.get("walltime", "") or "-"),
                ("Nodes", str(preset.get("nodes", "") or "-")),
                ("Tasks", str(preset.get("ntasks", "") or "-")),
                ("CPUs per task", str(preset.get("cpus_per_task", "") or "-")),
                ("Memory", preset.get("memory", "") or "-"),
                ("Modules", ", ".join(preset.get("modules") or []) or "-"),
                ("Pre-commands", "; ".join(preset.get("pre_commands") or []) or "-"),
                ("Extra directives", "; ".join(preset.get("extra_directives") or []) or "-"),
                ("Fetch patterns", ", ".join(preset.get("fetch_globs") or []) or "-"),
            ]
        if host is not None:
            rows += [
                ("", ""),
                ("Host target", host.target),
                ("Reads login files", "yes" if host.load_profile else "no"),
                ("Login commands", "; ".join(host.login_commands or []) or "-"),
            ]
        width = max(len(label) for label, _ in rows)
        lines = [f"{label.ljust(width)}  {value}".rstrip() for label, value in rows]
        # The script last and in full: it is the answer to "what actually ran",
        # and it is the thing worth copying into a terminal to try by hand.
        lines += ["", "--- script ---", job.command or "(not recorded)"]
        return "\n".join(lines)

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

    def _open_job_list_file(self) -> None:
        """Open a job list from anywhere, not only from the archive folder."""
        start = self.service.store.directory
        path, _ = QFileDialog.getOpenFileName(self, "Open a job list", start, JOB_LIST_FILTER)
        if path:
            self.open_job_list(path)

    def _rebuild_from_folder(self) -> None:
        """Make a job list out of results that are already on disk.

        For calculations this plugin never saw: fetched by hand, copied off a
        cluster, run before it was installed, or left behind by a list that was
        cleared. The records it writes are marked reconstructed, in the file
        itself, and nothing in such a list can be submitted or polled -- there
        is no host behind any of it.
        """
        start = (
            self.service.store.get_pref("last_rebuild_dir", "")
            or self.service.store.download_root()
        )
        folder = QFileDialog.getExistingDirectory(self, "Rebuild a job list from a folder", start)
        if not folder:
            return
        self.service.store.set_pref("last_rebuild_dir", folder)
        self.btn_rebuild.setEnabled(False)
        self._append_message(f"Reading {folder}...")

        from .folder_scan import scan_folder

        def work():
            # On a worker: a folder on a network share takes real time to walk,
            # and doing it here would freeze the window mid-scan.
            return scan_folder(folder)

        def done(result) -> None:
            self.btn_rebuild.setEnabled(True)
            self._use_rebuilt_list(folder, result)

        def failed(message: str) -> None:
            self.btn_rebuild.setEnabled(True)
            QMessageBox.warning(self, "Rebuild from folder", f"Could not read {folder}:\n{message}")

        run_async(self.service.pool, work, on_success=done, on_error=failed)

    def _use_rebuilt_list(self, folder: str, result) -> None:
        """Write what the scan found and switch the table to it."""
        from .folder_scan import summarise

        if not result.jobs:
            QMessageBox.information(
                self,
                "Rebuild from folder",
                f"No calculation outputs were found under:\n{folder}",
            )
            return
        counts = summarise(result)
        store = self.service.store
        # Saved in the folder that was scanned, beside the results it describes:
        # the list belongs to that folder, so copying or moving the folder takes
        # it along, and opening it again there needs no scan at all.
        name = f"rebuilt_{time.strftime('%Y%m%d_%H%M%S')}{JOB_EXTENSION}"
        path = os.path.join(folder, name)
        try:
            store.write_job_list(path, result.jobs, reconstructed=True)
        except OSError as exc:
            # A folder on a read-only share is a perfectly ordinary place to
            # find results, so falling back beats refusing.
            path = os.path.join(store.directory, name)
            try:
                store.write_job_list(path, result.jobs, reconstructed=True)
            except OSError:
                QMessageBox.warning(
                    self, "Rebuild from folder", f"Could not write the job list:\n{exc}"
                )
                return
        truncated = (
            f"\n\nOnly the first {result.files_seen} files were read; narrow the folder for the rest."
            if result.truncated
            else ""
        )
        confirm = QMessageBox.question(
            self,
            "Rebuild from folder",
            f"Found {counts['jobs']} calculation(s) and {counts['files']} output file(s), "
            f"saved as {os.path.basename(path)} in that folder.\n\n"
            "Open it now? It is read only: results can be opened from it, but "
            "nothing in it can be submitted, cancelled or polled." + truncated,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            self._append_message(f"Rebuilt list saved to {path}")
            return
        self._exit_archive()
        count = store.use_jobs_file(path)
        self.service.jobs_changed.emit()
        self.service.poller.start()
        self._update_active_file()
        self._update_buttons()
        self._append_message(f"Rebuilt {count} job(s) from {folder}")

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
            self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Job Monitor")
            self.lbl_active_file.setVisible(False)
            self.btn_default_file.setVisible(False)
            return
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - {os.path.basename(store.jobs_path)}")
        if self.viewing_reconstructed():
            self.lbl_active_file.setText(
                f"<b>Rebuilt from a folder</b> — {store.jobs_path}. Read only: these "
                "calculations were found on disk, not submitted from here, so nothing "
                "in this list can be submitted, cancelled or polled."
            )
        else:
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
        if job is None:
            return
        from .output_file_dialog import OutputFileSelectorDialog

        # Check existing local files
        existing_local: List[str] = []
        for path in job.downloaded_files or []:
            if path and os.path.isfile(path) and path not in existing_local:
                existing_local.append(os.path.normpath(path))

        # If exactly 1 local file and no remote directory, open directly
        if len(existing_local) == 1 and not job.remote_dir:
            self.open_result_files(existing_local)
            return

        # Otherwise open the output file selector dialog
        dialog = OutputFileSelectorDialog(
            self.service,
            job,
            parent=self,
            on_open_callback=lambda path: self.open_result_files([path]),
        )
        dialog.exec()

    def _on_results_ready(self, job_id: str, paths: list) -> None:
        if not self.chk_auto_open.isChecked():
            return
        self.open_result_files(paths)

    def _log_name_for(self, paths: List[str]) -> str:
        """The wrapper log of whichever job these paths belong to, if known."""
        wanted = {os.path.normpath(p) for p in paths or []}
        for job in self.service.store.jobs.values():
            if wanted & {os.path.normpath(p) for p in (job.downloaded_files or [])}:
                return job.log_file
        return ""

    def open_result_files(self, paths: List[str]) -> None:
        """Hand the most interesting downloaded file to the host application."""
        target = pick_primary_result(paths, self._log_name_for(paths))
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
        # Several files, dropped plainly, become that many separate jobs --
        # the far more common reason to drop a pile of files here. Hold Shift
        # while dropping for the one job every one of them used to become.
        batch = len(files) > 1 and not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.open_submit_dialog(files=files, batch=batch)

    # --- lifecycle ----------------------------------------------------------

    def _teardown(self) -> None:
        """Let go of the service. Safe to call twice.

        Deregisters too, so a reopened window is a fresh, live instance;
        polling continues in the service, which outlives this dialog.
        """
        # Stop the elapsed ticker so it doesn't fire after the dialog is gone.
        if hasattr(self, "_ticker"):
            self._ticker.stop()
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


def pick_primary_result(paths: List[str], log_file: str = "") -> str:
    """The file to hand to the application, out of everything downloaded.

    Ranked by what an analyzer plugin is most likely to claim, and never this
    plugin's own wrapper log whatever else is there. Falls back to the first
    path so a download of one unrecognised file still opens it.
    """
    from .runner import primary_output

    return primary_output(paths, log_file) or (paths or [""])[0]


def clear_document(main_window) -> bool:
    """Empty the editor so a result opens onto a clean canvas.

    Which of the two things used to happen depended entirely on the file's
    extension. The built-in loaders for .xyz and .mol clear the whole document
    themselves -- and with the unsaved-changes check skipped, so an auto-open
    after a download threw away work with no prompt. A result claimed by an
    analyzer plugin instead (.out, .log) cleared nothing, so the previous
    molecule stayed on the 2D canvas beside a 3D view of the new one, two
    different structures presented as one document.

    Cleared here for every route, and *with* the check: the host asks about
    unsaved work as it would for File > New, and answering Cancel leaves both
    the document and the result alone.

    Returns True when the document is clear -- including when this host is too
    old to have the manager, where the openers behave exactly as they did.
    """
    manager = getattr(main_window, "edit_actions_manager", None)
    clear = getattr(manager, "clear_all", None)
    if not callable(clear):
        return True
    try:
        return clear() is not False
    except Exception:
        logging.debug("Job Manager: the document was not cleared", exc_info=True)
        return True


def open_in_host(path: str) -> bool:
    """Route a downloaded file through the application's own file openers.

    Reuses ``MainWindow.init_manager.load_command_line_file``, which walks the
    registered plugin file openers by priority (that is how the ORCA Result
    Analyzer claims ``.out``) before falling back to the built-in loaders --
    so no analyzer plugin needs to be hard-coded here.

    The document is cleared first -- see :func:`clear_document`.
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

    if not clear_document(main_window):
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
