"""Job monitor: the live table of tracked jobs.

A model/view table rather than a QTableWidget, so a poll result repaints the
affected rows instead of rebuilding every cell on a timer.
"""

from __future__ import annotations

from html import escape

import logging
import os
import time
from typing import Any, List, Optional


from PyQt6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QVariant,
)
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


from . import PLUGIN_VERSION
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
from .ui_actions import action_button, action_toolbar, make_action, populate_menu
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
)

#: Job lists this window opens -- archived or not. .json covers files written
#: before the extension existed.
JOB_LIST_EXTENSIONS = (JOB_EXTENSION, ".json")
JOB_LIST_FILTER = f"Job lists (*{JOB_EXTENSION} *.json);;All files (*)"

#: Used for the two banners above the table. Palette roles, not fixed pastels:
#: the old pair read as navy on near-white in a dark theme.
from .theme import CY_ACCENT2 as _ACCENT2  # noqa: E402 – after other imports

BANNER_STYLE = (
    f"background: palette(alternate-base); color: palette(text); "
    f"border: 1px solid palette(mid); border-left: 3px solid {_ACCENT2}; "
    "padding: 6px 10px; border-radius: 4px;"
)


#: Opened in this plugin's own text window, never handed to MoleditPy.
#: OpenBabel lists "txt" as an input format (one empty molecule per line), so
#: with the OpenBabel plugin installed MoleditPy read an output text file as
#: thousands of molecules on the GUI thread and stopped responding -- after
#: first clearing the user's document to make room for a structure that was
#: never coming.
TEXT_EXTENSIONS = (".txt",)
#: The most of a text file shown at once; the end is kept, since that is where
#: an output file says how it finished.
TEXT_VIEW_LIMIT = 5 * 1024 * 1024

FORCE_ACTION_TEXT = "Force Run Now"
RECHECK_ACTION_TEXT = "Re-check State"

COLUMNS = ("Name", "Host", "Queue ID", "State", "After", "Elapsed", "Submitted", "Updated")

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

    Qt paints selected text with HighlightedText and ignores the model's
    ForegroundRole while selected, so RUNNING/FAILED/etc. all rendered the
    same near-black. Overriding the palette colour, not the pen after the
    fact, is what actually takes effect in both states.
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
        # The cached set, not chain_blocker: asked twice per row per repaint,
        # and chain_blocker walks the whole chain each time.
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
                if job.is_terminal or job.state == STATE_RUNNING or job.started_at:
                    return format_duration(job.elapsed())
                return f"wait {format_duration(job.waiting())}"
            if column == 6:
                return format_stamp(job.submitted_at)
            if column == 7:
                return format_stamp(job.updated_at)
        elif role == Qt.ItemDataRole.UserRole:
            # The raw value behind the formatted text, so sorting is numeric
            # ("10m" vs "2m") rather than string order.
            column = index.column()
            if column == 5:
                return (
                    job.elapsed()
                    if (job.is_terminal or job.state == STATE_RUNNING or job.started_at)
                    else job.waiting()
                )
            if column == 6:
                return job.submitted_at
            if column == 7:
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
    sort, type to filter, without changing what the model itself holds.

    Sorting reads UserRole, not the formatted text ("10m" vs "2m 05s").
    Filtering matches any column, not only the job name.
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
            # A mismatched pair (QVariant() vs a real value) can happen
            # mid-reload; an approximate text order is no real loss.
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
        #: Without the job counts, which :meth:`_show_counts` puts in front.
        self._base_title = f"Job Manager {PLUGIN_VERSION} - Job Monitor"
        self._title_counts = ""
        self.setWindowTitle(self._base_title)
        make_independent(self)
        apply_theme(self)
        self.resize(940, 560)
        #: Non-empty while a cleared list is being viewed read-only.
        self._archive_path = ""
        #: The open log window, if any; the tail goes there instead of the
        #: four-line strip at the bottom.
        self._tail_dialog: Optional[QDialog] = None
        self._host_monitor: Optional[QDialog] = None
        self._detail_dialogs: List[QDialog] = []
        self.setAcceptDrops(True)
        self._busy_actions: set[str] = set()
        self.model = JobTableModel(service, self)
        self._build_ui()
        self._connect_service()
        self._update_actions()
        # Elapsed is only redrawn when the model changes, which is on a poll
        # result -- without a separate repaint it advanced in jumps.
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick_elapsed)
        self._ticker.start()
        self._attach_presence()

    # --- construction -------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self._create_actions()
        self._build_menus(layout)
        self._build_toolbar(layout)
        self._build_banners(layout)
        self._build_filter(layout)
        self._build_table(layout)
        self._build_job_toolbar(layout)
        self.lbl_status = QLabel("")
        layout.addWidget(self.lbl_status)

    def _create_actions(self) -> None:
        specs = (
            ("new", "New Job...", self.open_submit_dialog, "Submit a new calculation."),
            ("hosts", "Hosts...", self.open_hosts_dialog, "Manage host profiles."),
            (
                "refresh",
                "Refresh Now",
                self._refresh_now,
                "Ask every host with active jobs for their status now.",
            ),
            (
                "reload",
                "Reload List",
                self._reload_jobs,
                "Re-read the job file to pick up changes from another Job Manager window.",
            ),
            (
                "host_monitor",
                "Host Monitor...",
                self.open_host_monitor,
                "Live load and memory per host, sampled only while that window is open.",
            ),
            (
                "settings",
                "Settings...",
                self.open_settings,
                "Polling, results, notifications, the task bar and the tray, the local API.",
            ),
            ("cancel", "Cancel Job", self._cancel_selected, "Cancel the selected job on its host."),
            ("download", "Download", self._download_selected, "Choose results to download."),
            (
                "open",
                "Open Result",
                self._open_selected_result,
                "Open one of this job's output files in MoleditPy.",
            ),
            (
                "tail",
                "Tail Log",
                self._tail_selected,
                "Read the end of the job's log in a window of its own.",
            ),
            (
                "tail_file",
                "Tail File...",
                self._tail_specific_file,
                "Read the tail of a chosen remote output/log file in the job's directory.",
            ),
            (
                "details",
                "Details",
                self._show_details,
                "Everything recorded about this job, and the script that ran.",
            ),
            (
                "resubmit",
                "Resubmit",
                self._resubmit_selected,
                "Open the submit wizard with this job's host, resources and input files.",
            ),
            ("remove", "Remove", self._remove_selected, "Remove the selected job from this list."),
            (
                "open_default",
                "Default List",
                self._use_default_job_list,
                "Back to the job list this plugin keeps in ~/.moleditpy/job_manager/.",
            ),
            (
                "open_list",
                "Open List...",
                self._open_job_list_file,
                f"Open a saved job list ({JOB_EXTENSION}). A cleared list opens read only.",
            ),
            (
                "save_as",
                "Save As...",
                lambda: self._export(JOB_EXTENSION),
                f"Save the job list to a {JOB_EXTENSION} file, openable again from here.",
            ),
            (
                "export_csv",
                "Export CSV...",
                lambda: self._export(".csv"),
                "Write one row per job: state, exit code, timings, paths.",
            ),
            (
                "rebuild",
                "Rebuild from Folder...",
                self._rebuild_from_folder,
                "Build a read-only job list from results already on disk.",
            ),
            (
                "archive",
                "Load Archive...",
                self._load_archive,
                "View a previously cleared job list, read only.",
            ),
            (
                "clear",
                "Clear List...",
                self._clear_jobs,
                "Empty the table, saving a dated copy first. Nothing on the host is deleted.",
            ),
            (
                "force",
                FORCE_ACTION_TEXT,
                self._force_selected,
                "Start a waiting helper-queue job now.",
            ),
            (
                "recheck",
                RECHECK_ACTION_TEXT,
                self._recheck_selected,
                "Look for evidence of a lost job.",
            ),
        )
        self.job_actions: dict[str, QAction] = {
            key: make_action(self, text, callback, tooltip)
            for key, text, callback, tooltip in specs
        }
        for key, shortcut in (
            ("new", "Ctrl+N"),
            ("open_list", "Ctrl+O"),
            ("save_as", "Ctrl+Shift+S"),
            ("refresh", "F5"),
            ("reload", "Ctrl+R"),
        ):
            self.job_actions[key].setShortcut(shortcut)
        self._job_menu_actions = tuple(
            self.job_actions[key] if key is not None else None
            for key in (
                "open",
                "download",
                "tail",
                "tail_file",
                "details",
                None,
                "resubmit",
                "force",
                "recheck",
                None,
                "cancel",
                "remove",
            )
        )

    def _build_menus(self, layout: QVBoxLayout) -> None:
        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        layout.setMenuBar(self.menu_bar)
        for label, keys in (
            (
                "&File",
                (
                    "open_default",
                    "open_list",
                    "archive",
                    "rebuild",
                    None,
                    "save_as",
                    "export_csv",
                    None,
                    "clear",
                ),
            ),
            ("&View", ("refresh", "reload", "host_monitor")),
            ("&Tools", ("hosts", "settings")),
        ):
            menu = self.menu_bar.addMenu(label)
            populate_menu(menu, (self.job_actions[key] if key else None for key in keys))
        self.menu_job = QMenu("&Job", self)
        populate_menu(self.menu_job, self._job_menu_actions)
        self.menu_bar.insertMenu(self.menu_bar.actions()[1], self.menu_job)

    def _build_toolbar(self, layout: QVBoxLayout) -> None:
        self.toolbar = action_toolbar(
            self, (self.job_actions[key] for key in ("new", "refresh", "host_monitor"))
        )
        self.btn_new = self.toolbar.widgetForAction(self.job_actions["new"])
        self.btn_refresh = self.toolbar.widgetForAction(self.job_actions["refresh"])
        self.btn_host_monitor = self.toolbar.widgetForAction(self.job_actions["host_monitor"])
        layout.addWidget(self.toolbar)

    def _build_banners(self, layout: QVBoxLayout) -> None:
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
        self.btn_default_file = action_button(self.job_actions["open_default"], self)
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

    def _build_filter(self, layout: QVBoxLayout) -> None:
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter"))
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("Filter jobs by name, host, queue id or state...")
        self.txt_filter.setClearButtonEnabled(True)
        self.txt_filter.textChanged.connect(self._apply_job_filter)
        filter_row.addWidget(self.txt_filter, 1)
        layout.addLayout(filter_row)

    def _build_table(self, layout: QVBoxLayout) -> None:
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableView()
        # A proxy between the table and the model: JobTableModel stays the
        # plain, testable read-only view of the store, addressed by source
        # row everywhere else (job_at, row_of, the elapsed ticker).
        self.proxy = JobFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicator(
            COLUMNS.index("Submitted"), Qt.SortOrder.DescendingOrder
        )
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
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # Stretching Name consumed only the space left by the other columns,
        # so it collapsed to an ellipsis in a narrow monitor.
        for column, width in enumerate((180, 100, 75, 90, 90, 85, 115, 115)):
            header.resizeSection(column, width)
        self.table.setItemDelegateForColumn(3, _StateColorDelegate(self.table))
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())
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

    def _build_job_toolbar(self, layout: QVBoxLayout) -> None:
        self.job_toolbar = action_toolbar(
            self, (self.job_actions[key] for key in ("open", "download", "tail", "details"))
        )
        self.btn_open = self.job_toolbar.widgetForAction(self.job_actions["open"])
        self.btn_download = self.job_toolbar.widgetForAction(self.job_actions["download"])
        self.btn_tail = self.job_toolbar.widgetForAction(self.job_actions["tail"])
        self.btn_details = self.job_toolbar.widgetForAction(self.job_actions["details"])
        spacer = QWidget(self.job_toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.job_toolbar.addWidget(spacer)
        self.btn_job_actions = QToolButton(self)
        self.btn_job_actions.setProperty("jobManagerAction", True)
        self.btn_job_actions.setText("Job Actions")
        self.btn_job_actions.setMenu(self.menu_job)
        self.btn_job_actions.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.job_toolbar.addWidget(self.btn_job_actions)
        layout.addWidget(self.job_toolbar)

    def _connect_service(self) -> None:
        # Kept as a list so closeEvent can undo every one: the service outlives
        # this window, and a leaked connection keeps reloading a dead dialog.
        self._connections = [
            (self.service.jobs_changed, self.model.reload),
            (self.service.jobs_changed, self._update_actions),
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
        self._update_actions()

    def open_settings(self) -> None:
        """Every standing preference, in one window. See settings_dialog.py."""
        from .settings_dialog import SettingsDialog

        SettingsDialog(self.service, self).exec()

    # --- helpers ------------------------------------------------------------

    def viewing_archive(self) -> bool:
        return bool(self._archive_path)

    def _apply_job_filter(self, text: str) -> None:
        self.proxy.set_search_text(text)

    def selected_job(self) -> Optional[Job]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        # The view's model is the sort/filter proxy, so map back to the
        # source model that actually holds the job.
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
        self.txt_log.setPlainText(text)

    def _show_row_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if index.isValid():
            self.table.selectRow(index.row())
        self._update_actions()
        if self.selected_job() is None:
            return
        self.menu_job.exec(self.table.viewport().mapToGlobal(position))

    def _tick_elapsed(self) -> None:
        """Repaint the Elapsed cell of every job that is still going."""
        if not self.isVisible():
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

    def _can_open_result(self, job: Optional[Job]) -> bool:
        if job is None:
            return False
        if job.downloaded_files or (job.downloaded and job.remote_dir):
            return True
        host = self.service.store.hosts.get(job.host_id)
        mirror = host.mirrored_job_dir(job.remote_dir) if host and job.remote_dir else ""
        return bool(mirror and os.path.isdir(mirror))

    def _update_actions(self) -> None:
        job = self.selected_job()
        archived = self.viewing_archive()
        reconstructed = self.viewing_reconstructed() and not archived
        live = not archived and not reconstructed
        remote = bool(live and job and job.remote_dir)
        states = {
            "new": not reconstructed,
            "reload": live,
            "cancel": bool(live and job and job.is_active),
            "download": remote,
            "tail": remote,
            "tail_file": remote,
            "open": bool(
                not archived
                and (
                    (reconstructed and job and job.downloaded_files)
                    or (live and self._can_open_result(job))
                )
            ),
            "details": job is not None,
            "resubmit": bool(live and job and (job.input_files or job.preset)),
            "remove": not archived and job is not None,
            "save_as": not archived,
            "export_csv": not archived,
            "clear": not archived,
            "force": bool(live and job and not self.service.force_refusal(job)),
            "recheck": bool(live and job and job.state == STATE_LOST),
        }
        for key, action in self.job_actions.items():
            action.setEnabled(bool(states.get(key, True) and key not in self._busy_actions))
        self.btn_job_actions.setEnabled(job is not None)

    def _set_action_busy(self, key: str, busy: bool) -> None:
        if busy:
            self._busy_actions.add(key)
        else:
            self._busy_actions.discard(key)
        self._update_actions()

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
            # Also reached from a drop: a rebuilt list has nowhere to submit to.
            QMessageBox.information(
                self,
                "Job Manager",
                "This job list was rebuilt from a folder, so it is read only.\n\n"
                "Choose File > Default List to go back to your own list before submitting.",
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
            # Prefill can't select a host that no longer exists; the wizard
            # would silently open on whichever host sorts first.
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
            # A job that ran on work already staged on the host resubmits
            # against that same directory; there is no local input to send.
            remote_dir=job.remote_dir if job.remote_dir_provided else "",
            remote_input=job.remote_input,
        )

    def open_host_monitor(self) -> None:
        """Open the live host panel, or raise the one already up."""
        from . import HOST_MONITOR_WINDOW_KEY, get_context
        from .host_monitor import HostMonitorDialog, find_open
        from .window_utils import bring_to_front

        # One Host Monitor, whichever way it was opened. Extensions > Job
        # Manager > Host Monitor registers its window under this key; this
        # button used to keep its own, so both could be open at once, each
        # sampling every host over SSH.
        context = get_context()
        existing = self._host_monitor
        if existing is None and context is not None:
            existing = context.get_window(HOST_MONITOR_WINDOW_KEY)
        if existing is None:
            existing = find_open(self.service)
        if existing is not None:
            bring_to_front(existing)
            return
        dialog = HostMonitorDialog(self.service, parent=None)
        self._host_monitor = dialog
        dialog.finished.connect(lambda *_: setattr(self, "_host_monitor", None))
        if context is not None:
            context.register_window(HOST_MONITOR_WINDOW_KEY, dialog)
            dialog.finished.connect(
                lambda *_: context.register_window(HOST_MONITOR_WINDOW_KEY, None)
            )
        dialog.show()

    def open_hosts_dialog(self) -> None:
        from .hosts_dialog import HostsDialog

        dialog = HostsDialog(self.service, self)
        dialog.exec()

    def _refresh_now(self) -> None:
        if not self.service.poller.refresh_now():
            self._append_message("Refresh is rate limited; try again in a few seconds.")

    def _reload_jobs(self) -> None:
        """Take in what another Job Manager instance changed on disk.

        Separate from Refresh Now, which asks the *hosts*: two windows share one
        job file and never see each other's writes until one of them re-reads
        it. Not rate limited -- this is a file read, not a login node.
        """
        selected = self.selected_job()
        selected_id = selected.id if selected is not None else ""
        result = self.service.reload_jobs()
        self._append_message(result.summary())
        if selected_id:
            # jobs_changed resets the model, which drops the selection; put it
            # back so a reload does not lose the row the user was working on.
            self._select_job(selected_id)

    def _select_job(self, job_id: str) -> None:
        """Re-select ``job_id`` if it is still in the table."""
        row = self.model.row_of(job_id)
        if row < 0:
            return
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        if index.isValid():
            self.table.selectRow(index.row())

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
        # A chain makes "cancel" ambiguous -- one job, or it and everything
        # queued behind it -- so ask outright rather than decide.
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
            # Behind first, so nothing starts by being released while the
            # chain is still being taken down.
            for dependent in reversed(dependents):
                self.service.cancel(dependent, release_dependents=False)
            self.service.cancel(job, release_dependents=False)
            return
        self.service.cancel(job)

    def _force_selected(self) -> None:
        """Start the selected waiting job now, ahead of its host's queue."""
        job = self.selected_job()
        if job is None:
            return
        refusal = self.service.force_refusal(job)
        if refusal:
            QMessageBox.information(self, FORCE_ACTION_TEXT, refusal)
            return
        confirm = QMessageBox.question(
            self,
            FORCE_ACTION_TEXT,
            f"Start '{job.name}' now, ahead of the jobs waiting on {job.host_name}?\n\n"
            "It runs beside whatever is running there already, past the host's job "
            "limit and its core and memory budgets. Meant for a small, short job.",
        )
        if confirm != QMessageBox.StandardButton.Yes or not self._has_credentials(job):
            return
        self.service.force_run(job)

    def _recheck_selected(self) -> None:
        """Ask the host again about a job that was reported LOST."""
        job = self.selected_job()
        if job is None or job.state != STATE_LOST or not self._has_credentials(job):
            return
        self._append_message(f"Re-checking {job.name} on {job.host_name}...")
        self.service.recheck(job, on_done=self._show_recheck, owner=self)

    def _show_recheck(self, report: dict) -> None:
        state = report.get("state", STATE_LOST)
        lines = []
        if not report.get("changed"):
            lines.append(
                "Still no sign that it finished: there is no exit code for it on the "
                "host, neither the job's own nor one the helper queue recorded."
            )
        elif state in (STATE_DONE, STATE_FAILED):
            lines.append(
                f"The host does have an exit code for it ({report.get('rc')}), so it is "
                f"now {state}."
            )
        else:
            lines.append(f"It is still in the queue on the host: it is now {state}.")
        lines.append("")
        lines.append(f"Exit-code file: {report.get('sentinel') or '-'}")
        if report.get("runner_status"):
            lines.append(f"Helper queue's record: {report.get('runner_status')}")
        files = list(report.get("files") or [])
        if files:
            shown = ", ".join(files[:12]) + (
                f", and {len(files) - 12} more" if len(files) > 12 else ""
            )
            lines.append(f"In its directory: {shown}")
        else:
            lines.append("Its directory on the host is empty or gone.")
        QMessageBox.information(self, RECHECK_ACTION_TEXT, "\n".join(lines))

    def _has_credentials(self, job: Job) -> bool:
        """Prompt for this job's host password before any worker is dispatched."""
        host = self.service.store.hosts.get(job.host_id)
        if host is None:
            return True  # the service reports the missing profile itself
        return ensure_password(self.service, host, self)

    def _download_selected(self) -> None:
        """Show what is on the host and let the user pick which files, and
        into which folder. Nothing is fetched until they say so."""
        job = self.selected_job()
        if job is None or not self._has_credentials(job):
            return
        self._set_action_busy("download", True)
        self._append_message(f"Listing {job.remote_dir}...")

        def listed(names: list) -> None:
            self._set_action_busy("download", False)
            self._offer_download(job, names)

        def failed(message: str) -> None:
            self._set_action_busy("download", False)
            self._append_message(message)

        self.service.list_remote_results(job, listed, failed, owner=self)

    def _offer_download(self, job: Job, names: list) -> None:
        from .download_dialog import DownloadDialog
        from .runner import is_plugin_file, likely_outputs, select_files

        matched = [
            name
            for name in select_files(names, job.fetch_globs or [])
            # Offered, never pre-ticked: `*.log` matches Gaussian's own output
            # too, and ticking the wrapper's log would defeat the point.
            if not is_plugin_file(name, job.log_file)
        ]
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
        """Tail the log while a job runs; open the result once it has finished."""
        job = self.selected_job()
        if job is None:
            return
        if job.is_terminal and self.job_actions["open"].isEnabled():
            self._open_selected_result()
            return
        if self.job_actions["tail"].isEnabled():
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
            self._tail_dialog.present()
        else:
            # Both title and refresh callback, so auto-refresh follows the
            # newly selected job rather than the old one.
            self._tail_dialog.setWindowTitle(
                f"Job Manager {PLUGIN_VERSION} - {job.name}: {job.log_file}"
            )
            self._tail_dialog.set_refresh(lambda: self.service.tail(job))
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

            # The calculation's own output, never the wrapper's log -- that is
            # what the Tail Log button already opens. Still in the list, since
            # asking for it deliberately is allowed; just not preselected.
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

        self.service.list_remote_results(job, on_files_listed, on_list_error, owner=self)

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
            lambda *_: (
                self._detail_dialogs.remove(dialog) if dialog in self._detail_dialogs else None
            )
        )
        dialog.present()
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

        self.service.tail_file(job, filename, on_done=on_done, on_error=on_err, owner=self)

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
        # finished can arrive more than once for one window; a second
        # remove() raised ValueError out of a Qt slot, reported as a crash.
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
        if job.force_run:
            rows.insert(5, ("Force run", "started ahead of the queue"))
        # The snapshot taken at submit time, not the named preset -- which may
        # since have been edited or deleted.
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
                ("Submit options", preset.get("submit_options", "") or "-"),
                ("Fetch patterns", ", ".join(preset.get("fetch_globs") or []) or "-"),
            ]
        if host is not None:
            rows += [
                ("", ""),
                ("Host target", host.target),
                ("Reads login files", "yes" if host.load_profile else "no"),
                ("Login commands", "; ".join(host.login_commands or []) or "-"),
                ("Host submit options", host.submit_options or "-"),
            ]
        width = max(len(label) for label, _ in rows)
        lines = [f"{label.ljust(width)}  {value}".rstrip() for label, value in rows]
        # Last and in full: the thing worth copying into a terminal by hand.
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
        """Make a job list out of results already on disk, for calculations
        this plugin never saw. Marked reconstructed; nothing in it can be
        submitted or polled."""
        start = (
            self.service.store.get_pref("last_rebuild_dir", "")
            or self.service.store.download_root()
        )
        folder = QFileDialog.getExistingDirectory(self, "Rebuild a job list from a folder", start)
        if not folder:
            return
        self.service.store.set_pref("last_rebuild_dir", folder)
        self._set_action_busy("rebuild", True)
        self._append_message(f"Reading {folder}...")

        from .folder_scan import scan_folder

        def work():
            # On a worker: a network-share folder takes real time to walk.
            return scan_folder(folder)

        def done(result) -> None:
            self._set_action_busy("rebuild", False)
            self._use_rebuilt_list(folder, result)

        def failed(message: str) -> None:
            self._set_action_busy("rebuild", False)
            QMessageBox.warning(self, "Rebuild from folder", f"Could not read {folder}:\n{message}")

        run_async(self.service.pool, work, on_success=done, on_error=failed, owner=self)

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
        # Saved beside the results it describes, so moving the folder takes
        # the list along and reopening it there needs no rescan.
        name = f"rebuilt_{time.strftime('%Y%m%d_%H%M%S')}{JOB_EXTENSION}"
        path = os.path.join(folder, name)
        try:
            store.write_job_list(path, result.jobs, reconstructed=True)
        except OSError as exc:
            # A read-only share is an ordinary place to find results.
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
        self._update_actions()
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
        """Open a job list: read-only if the file says it is archived (the
        flag travels with the file, so a cleared list stays history)."""
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
        # Leave the read-only view first, or the table keeps showing the
        # archive while tracking and saving have already moved on.
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
            self._set_base_title(f"Job Manager {PLUGIN_VERSION} - Job Monitor")
            self.lbl_active_file.setVisible(False)
            self.btn_default_file.setVisible(False)
            return
        self._set_base_title(f"Job Manager {PLUGIN_VERSION} - {os.path.basename(store.jobs_path)}")
        if self.viewing_reconstructed():
            self.lbl_active_file.setText(
                f"<b>Rebuilt from a folder</b> — {escape(str(store.jobs_path))}. Read only: these "
                "calculations were found on disk, not submitted from here, so nothing "
                "in this list can be submitted, cancelled or polled."
            )
        else:
            self.lbl_active_file.setText(
                f"Working in <b>{escape(str(store.jobs_path))}</b> for this session. "
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
            f"Viewing <b>{escape(os.path.basename(path))}</b> ({len(jobs)} jobs) — this list is "
            "marked archived, so it is read only. To delete archives permanently, "
            f"open {directory}"
        )
        self.lbl_archive.setVisible(True)
        self.btn_back.setVisible(True)
        self._update_actions()
        self._append_message(f"Viewing {os.path.basename(path)} (read only)")
        return True

    def _exit_archive(self) -> None:
        """Back to the live job list."""
        self._archive_path = ""
        self.model.show_archive(None)
        self.lbl_archive.setVisible(False)
        self.btn_back.setVisible(False)
        self._update_actions()

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

        # Normalised before the duplicate check, so one file listed two ways
        # counts once -- and a single result opens without the chooser.
        existing_local: List[str] = []
        for path in job.downloaded_files or []:
            path = os.path.normpath(path) if path else ""
            if path and os.path.isfile(path) and path not in existing_local:
                existing_local.append(path)

        if len(existing_local) == 1 and not job.remote_dir:
            self.open_result_files(existing_local)
            return

        dialog = OutputFileSelectorDialog(
            self.service,
            job,
            parent=self,
            on_open_callback=lambda path: self.open_result_files([path]),
            on_text_callback=self.show_text_file,
        )
        dialog.exec()

    def _on_results_ready(self, job_id: str, paths: list) -> None:
        if not self.service.store.get_pref("open_result_after_download", True):
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
        if is_text_file(target):
            self.show_text_file(target)
            return
        from . import get_context

        if open_in_host(target):
            self._append_message(f"Opened {os.path.basename(target)}")
        elif get_context() is None:
            # Run on its own there is no MoleditPy to hand it to; saying only
            # "Downloaded" read as the Open button having done nothing.
            self._append_message(f"No MoleditPy in this process to open it in; it is at {target}")
        else:
            self._append_message(f"Downloaded {target}")

    def show_text_file(self, path: str) -> None:
        """A file as plain text, in a window of this plugin's own."""
        try:
            dialog = show_text_window(path, self)
        except OSError as exc:
            self._append_error(f"Could not read {path}: {exc}")
            return
        self._detail_dialogs.append(dialog)
        dialog.finished.connect(
            lambda *_: (
                self._detail_dialogs.remove(dialog) if dialog in self._detail_dialogs else None
            )
        )
        self._append_message(f"Opened {os.path.basename(path)}")

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
        # toLocalFile() returns forward slashes on Windows; normalise once.
        return os.path.normpath(path)

    @staticmethod
    def _dropped_input_files(event) -> List[str]:
        """Local files that are not a job list, i.e. things to submit.

        Input extensions are not registered with the host application-wide:
        that would take ``.inp``/``.xyz`` away from being *opened* on the
        main window.
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
        # Anything else that is a real file opens the wizard prefilled.
        files = self._dropped_input_files(event)
        if not files:
            event.ignore()
            return
        event.acceptProposedAction()
        # Several files dropped plainly become that many separate jobs; hold
        # Shift for the one-job case.
        batch = len(files) > 1 and not bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.open_submit_dialog(files=files, batch=batch)

    # --- the task bar and the title ------------------------------------------

    def _attach_presence(self) -> None:
        """Counts in the title, progress and buttons on the task bar button."""
        from . import presence, win_taskbar

        self._presence = presence.current()
        style = self.style()
        pixmap = style.StandardPixmap
        self._taskbar = win_taskbar.WindowTaskbar(
            self,
            [
                (1, style.standardIcon(pixmap.SP_BrowserReload), "Refresh now", self._refresh_now),
                (2, style.standardIcon(pixmap.SP_FileIcon), "New job", self._new_job_from_taskbar),
                (
                    3,
                    style.standardIcon(pixmap.SP_ComputerIcon),
                    "Host monitor",
                    self.open_host_monitor,
                ),
            ],
        )
        if self._presence is not None:
            self._presence.add_title_listener(self._show_counts)
            self._presence.add_window(self._taskbar)
        else:
            self._show_counts(presence.count_jobs(self.service.store))

    def _detach_presence(self) -> None:
        current = getattr(self, "_presence", None)
        if current is not None:
            current.remove_title_listener(self._show_counts)
            current.remove_window(self._taskbar)
        self._presence = None
        # With or without a presence: the thumbnail icons are this window's.
        taskbar = getattr(self, "_taskbar", None)
        if taskbar is not None:
            taskbar.release()

    def _new_job_from_taskbar(self) -> None:
        from .window_utils import bring_to_front

        bring_to_front(self)
        self.open_submit_dialog()

    def _set_base_title(self, title: str) -> None:
        self._base_title = title
        self.setWindowTitle(self._title_counts + title)

    def _show_counts(self, counts: dict) -> None:
        """Counts first: the task bar and Alt+Tab cut a long title from the end."""
        from .presence import title_prefix

        self._title_counts = title_prefix(counts)
        self.setWindowTitle(self._title_counts + self._base_title)

    def select_job(self, job_id: str) -> None:
        """Select ``job_id``, clearing a filter that hides it."""
        row = self.model.row_of(job_id)
        if row < 0:
            return
        if not self.proxy.mapFromSource(self.model.index(row, 0)).isValid():
            self.txt_filter.clear()
        self._select_job(job_id)
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        if index.isValid():
            self.table.scrollTo(index)

    def nativeEvent(self, event_type, message):  # noqa: N802 - Qt's spelling
        taskbar = getattr(self, "_taskbar", None)
        if taskbar is not None and event_type == b"windows_generic_MSG":
            try:
                if taskbar.handle(message):
                    return True, 0
            except Exception:
                logging.debug("Job Manager: task bar message not handled", exc_info=True)
        # Not super().nativeEvent(): under PyQt6 6.11 on Windows, handing its
        # result back crashes Qt with an access violation on the window's very
        # first message. QWidget's own implementation only returns false.
        return False, 0

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        # Looking at the monitor is what "seen" means for a failed job: the
        # tray and the task bar stop showing red once it has been in front.
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            current = getattr(self, "_presence", None)
            if current is not None:
                current.acknowledge()
        super().changeEvent(event)

    # --- lifecycle ----------------------------------------------------------

    def _teardown(self) -> None:
        """Let go of the service and deregister. Safe to call twice; polling
        continues in the service, which outlives this dialog."""
        if hasattr(self, "_ticker"):
            self._ticker.stop()
        self._detach_presence()
        self._disconnect_service()
        try:
            from . import forget_window

            forget_window()
        except Exception:
            logging.debug("Job Manager: window deregistration failed", exc_info=True)

    def reject(self) -> None:
        # Esc closes a QDialog through reject(), which never reaches
        # closeEvent, so teardown must happen here too.
        self._teardown()
        super().reject()

    def closeEvent(self, event) -> None:
        self._teardown()
        # Accepted, not delegated: QDialog's closeEvent calls reject(), which
        # now tears down as well -- doing both would recurse.
        event.accept()


def is_text_file(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in TEXT_EXTENSIONS


def show_text_window(path: str, parent: Optional[QWidget] = None):
    """Open ``path`` read-only in a text window, and return the window."""
    from .text_dialog import TextDialog

    def reload() -> None:
        try:
            dialog.set_text(read_text_for_view(path))
        except OSError as exc:
            dialog.set_text(f"Could not read {path}: {exc}")

    # A finished result is read from the top; only a tail starts at the end.
    dialog = TextDialog(
        f"Job Manager {PLUGIN_VERSION} - {os.path.basename(path)}",
        "",
        parent,
        on_refresh=reload,
        follow=False,
        auto_refresh=False,
    )
    dialog.set_text(read_text_for_view(path))
    dialog.present()
    return dialog


def read_text_for_view(path: str, limit: int = TEXT_VIEW_LIMIT) -> str:
    """The file as text, or its last ``limit`` bytes with a line saying so."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        data = handle.read()
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if size > limit:
        # Cut at a line boundary, so the first line shown is a whole one.
        text = text.split("\n", 1)[-1]
        text = f"[Showing the last {limit // (1024 * 1024)} MB of {size:,} bytes]\n\n" + text
    return text


def pick_primary_result(paths: List[str], log_file: str = "") -> str:
    """The file to hand to the application: ranked by what an analyzer plugin
    is most likely to claim, never this plugin's own wrapper log, falling
    back to the first path."""
    from .runner import primary_output

    return primary_output(paths, log_file) or (paths or [""])[0]


def clear_document(main_window) -> bool:
    """Empty the editor so a result opens onto a clean canvas.

    Used to depend on the file's extension: built-in .xyz/.mol loaders
    cleared with the unsaved-changes check skipped (silent data loss), while
    an analyzer plugin (.out, .log) cleared nothing (two molecules on screen
    at once). Cleared here for every route, *with* the check.

    Returns True when the document is clear, including on a host too old to
    have this manager.
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

    Reuses ``MainWindow.init_manager.load_command_line_file``, which walks
    registered plugin openers by priority before the built-in loaders, so no
    analyzer plugin needs to be hard-coded here. Clears the document first --
    see :func:`clear_document`.
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
