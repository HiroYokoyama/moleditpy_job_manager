"""Job monitor table presentation, sorting and filtering."""

from __future__ import annotations

import time
from typing import List, Optional

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    QVariant,
)
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QStyledItemDelegate,
    QWidget,
)

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
)

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
