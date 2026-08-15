"""Output file selector dialog.

Allows users to choose which output file from a job to open in MoleditPy.
Files must be downloaded locally before they can be opened.
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable, List, Optional, Sequence

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .models import Job
from .theme import apply_theme

PATH_ROLE = Qt.ItemDataRole.UserRole
IS_REMOTE_ROLE = Qt.ItemDataRole.UserRole + 1

_TYPE_MAP = {
    ".out": "Output File (ORCA / Q-Chem / GAMESS)",
    ".log": "Log File (Gaussian / ORCA)",
    ".fchk": "Formatted Checkpoint File",
    ".chk": "Checkpoint File",
    ".xyz": "XYZ Coordinates",
    ".hess": "Hessian Matrix File",
    ".molden": "Molden Orbital File",
    ".cube": "Gaussian Cube Grid",
    ".dat": "Data File",
    ".json": "JSON Results",
    ".csv": "CSV Spreadsheet",
    ".txt": "Text File",
    ".inp": "Input File",
    ".com": "Gaussian Input File",
}


def describe_file_type(filename: str) -> str:
    """Return a human-readable file category for the given filename."""
    ext = os.path.splitext(filename)[1].lower()
    return _TYPE_MAP.get(ext, f"{ext.upper().lstrip('.')} File" if ext else "File")


def format_file_size(num_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


class OutputFileSelectorDialog(QDialog):
    """Dialog allowing the user to select and open any output file from a job."""

    def __init__(
        self,
        service,
        job: Job,
        parent: Optional[QWidget] = None,
        on_open_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.job = job
        self.on_open_callback = on_open_callback
        self._all_items: List[QTreeWidgetItem] = []

        self.setWindowTitle(f"Open Output File — {job.name}")
        apply_theme(self)
        self.resize(650, 480)

        self._build_ui()
        self._load_files()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)
        self.lbl_title = QLabel(f"<b>Job: {self.job.name}</b>")
        self.lbl_subtitle = QLabel("Select an output file to open in MoleditPy:")
        header_layout.addWidget(self.lbl_title)
        header_layout.addWidget(self.lbl_subtitle)
        layout.addLayout(header_layout)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(QLabel("Filter:"))
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("Type to filter files (e.g. .out, .xyz)...")
        self.txt_filter.setClearButtonEnabled(True)
        self.txt_filter.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.txt_filter, 1)
        layout.addLayout(filter_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File Name", "Size", "Type", "Location"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(lambda item, _: self._open_item(item))
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.tree, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self.lbl_status)

        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(6)

        self.btn_open_folder = QPushButton("Open Containing Folder")
        self.btn_open_folder.setToolTip("Open the local directory holding downloaded outputs.")
        self.btn_open_folder.clicked.connect(self._open_containing_folder)
        bottom_bar.addWidget(self.btn_open_folder)

        self.btn_browse_remote = QPushButton("Browse Host Files...")
        self.btn_browse_remote.setToolTip("Query the remote host directory for all files.")
        self.btn_browse_remote.clicked.connect(self._fetch_remote_listing)
        bottom_bar.addWidget(self.btn_browse_remote)

        bottom_bar.addStretch(1)

        self.btn_open = QPushButton("Open")
        self.btn_open.setDefault(True)
        self.btn_open.clicked.connect(self._open_selected)
        bottom_bar.addWidget(self.btn_open)

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        bottom_bar.addWidget(self.btn_close)

        layout.addLayout(bottom_bar)

    def _load_files(self) -> None:
        """Scan local files first; if none exist, fetch remote listing."""
        local_files = self._get_existing_local_files()
        if local_files:
            self._populate_tree_local(local_files)
        elif self.job.remote_dir:
            self._fetch_remote_listing()
        else:
            self.lbl_status.setText("No local files or remote directory recorded for this job.")

    def _get_existing_local_files(self) -> List[str]:
        """Find all local output files associated with this job."""
        files: List[str] = []
        for path in self.job.downloaded_files or []:
            if path and os.path.isfile(path) and path not in files:
                files.append(os.path.normpath(path))

        if self.job.local_dir and os.path.isdir(self.job.local_dir):
            try:
                for entry in os.listdir(self.job.local_dir):
                    full = os.path.normpath(os.path.join(self.job.local_dir, entry))
                    if os.path.isfile(full) and full not in files and not entry.startswith("."):
                        files.append(full)
            except OSError:
                pass

        cache_dir = os.path.join(tempfile.gettempdir(), "moleditpy_job_manager_cache", self.job.id)
        if os.path.isdir(cache_dir):
            try:
                for entry in os.listdir(cache_dir):
                    full = os.path.normpath(os.path.join(cache_dir, entry))
                    if os.path.isfile(full) and full not in files and not entry.startswith("."):
                        files.append(full)
            except OSError:
                pass

        return files

    def _populate_tree_local(self, paths: Sequence[str]) -> None:
        """Fill tree with local files."""
        self.tree.clear()
        self._all_items.clear()

        from .jobs_dialog import pick_primary_result

        primary_path = pick_primary_result(list(paths))
        selected_item: Optional[QTreeWidgetItem] = None

        for path in paths:
            name = os.path.basename(path)
            try:
                size_str = format_file_size(os.path.getsize(path))
            except OSError:
                size_str = "-"
            type_str = describe_file_type(name)
            location = "Local"

            item = QTreeWidgetItem([name, size_str, type_str, location])
            item.setData(0, PATH_ROLE, path)
            item.setData(0, IS_REMOTE_ROLE, False)
            item.setToolTip(0, path)

            self.tree.addTopLevelItem(item)
            self._all_items.append(item)

            if path == primary_path:
                selected_item = item

        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        elif self.tree.topLevelItemCount() > 0:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

        self.lbl_status.setText(f"{len(paths)} file(s) available locally.")
        self.btn_open_folder.setEnabled(True)

    def _fetch_remote_listing(self) -> None:
        """Query host for file list in the remote directory."""
        if not self.job.remote_dir:
            self.lbl_status.setText("No remote directory recorded for this job.")
            return

        self.lbl_status.setText("Listing files on host...")
        self.btn_browse_remote.setEnabled(False)

        def on_ok(names: list) -> None:
            self.btn_browse_remote.setEnabled(True)
            self._populate_tree_remote(names)

        def on_error(msg: str) -> None:
            self.btn_browse_remote.setEnabled(True)
            self.lbl_status.setText(f"Could not list remote files: {msg}")

        self.service.list_remote_results(self.job, on_ok, on_error)

    def _mirrored_job_dir(self) -> str:
        """Where this job's remote directory lives on disk, via the host's
        'equal path' setting -- or "" if the host has none configured.

        Best-effort: the host's remote root is stripped as a prefix of the
        job's remote directory to get the part underneath it; a job whose
        directory was not derived from the root (one the user pointed the
        wizard at directly) falls back to its last path segment, which is
        usually still right since the mirror and the host describe the same
        tree by construction.
        """
        from .models import HostProfile

        host = self.service.store.hosts.get(self.job.host_id)
        if not isinstance(host, HostProfile) or not host.equal_path:
            return ""
        try:
            remote_dir = str(self.job.remote_dir or "").replace("\\", "/").rstrip("/")
            remote_root = str(host.remote_root or "").replace("\\", "/").rstrip("/")
            if not remote_dir:
                return ""
            if remote_root and remote_dir.startswith(remote_root):
                rel = remote_dir[len(remote_root) :].lstrip("/")
            else:
                rel = remote_dir.rsplit("/", 1)[-1]
            return host.mirrored_path(rel)
        except Exception:
            # Best-effort: a malformed path here must never break the dialog
            # that lists what a job produced.
            return ""

    def _folder_item(self, cache: dict, parts: Sequence[str]) -> Optional[QTreeWidgetItem]:
        """The QTreeWidgetItem for a folder path, creating it (and its
        ancestors) on first use so files land under the same subfolders the
        host has them in."""
        if not parts:
            return None
        key = tuple(parts)
        if key in cache:
            return cache[key]
        parent = self._folder_item(cache, parts[:-1])
        item = QTreeWidgetItem([parts[-1], "", "Folder", ""])
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        item.setExpanded(True)
        cache[key] = item
        return item

    def _populate_tree_remote(self, names: Sequence[str]) -> None:
        """Populate the tree with remote files under the host's own folder
        structure, merging in any already-downloaded local files."""
        self.tree.clear()
        self._all_items.clear()

        local_files = self._get_existing_local_files()
        local_map = {os.path.basename(p): p for p in local_files}
        mirror_dir = self._mirrored_job_dir()

        from .jobs_dialog import pick_primary_result

        primary_name = os.path.basename(pick_primary_result(list(names)))
        selected_item: Optional[QTreeWidgetItem] = None
        folders: dict = {}
        gray = QColor("#8b949e")

        for name in names:
            if not name or name.startswith("."):
                continue
            parts = [p for p in name.replace("\\", "/").split("/") if p]
            if not parts:
                continue
            filename = parts[-1]
            parent = self._folder_item(folders, parts[:-1])

            mirrored_path = os.path.join(mirror_dir, *parts) if mirror_dir else ""
            if filename in local_map:
                local_path = local_map[filename]
                try:
                    size_str = format_file_size(os.path.getsize(local_path))
                except OSError:
                    size_str = "-"
                location = "Local"
                is_remote = False
                target_data = local_path
                tooltip = target_data
            elif mirrored_path and os.path.isfile(mirrored_path):
                try:
                    size_str = format_file_size(os.path.getsize(mirrored_path))
                except OSError:
                    size_str = "-"
                location = "Mirror (no download needed)"
                is_remote = False
                target_data = mirrored_path
                tooltip = f"Opened directly from the host's local mirror:\n{mirrored_path}"
            else:
                size_str = "-"
                location = "On Host (Not Downloaded)"
                is_remote = True
                target_data = name
                tooltip = f"Remote file: {name} (download the job, or open it, to fetch it)"

            item = QTreeWidgetItem([filename, size_str, describe_file_type(filename), location])
            item.setData(0, PATH_ROLE, target_data)
            item.setData(0, IS_REMOTE_ROLE, is_remote)
            item.setToolTip(0, tooltip)
            if is_remote:
                # Greyed rather than hidden: seeing what exists but is not
                # here yet is the point of listing the host at all.
                for column in range(4):
                    item.setForeground(column, gray)
                italic = item.font(0)
                italic.setItalic(True)
                item.setFont(0, italic)

            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            self._all_items.append(item)

            if filename == primary_name:
                selected_item = item

        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        elif self._all_items:
            self.tree.setCurrentItem(self._all_items[0])

        self.lbl_status.setText(f"{len(names)} remote file(s) found on host.")

    def _apply_filter(self, text: str) -> None:
        """Filter tree rows based on search input."""
        query = (text or "").strip().lower()
        for item in self._all_items:
            name = item.text(0).lower()
            file_type = item.text(2).lower()
            visible = not query or (query in name) or (query in file_type)
            item.setHidden(not visible)

    def _open_selected(self) -> None:
        item = self.tree.currentItem()
        if item is not None and not item.isHidden():
            self._open_item(item)

    def _open_item(self, item: QTreeWidgetItem) -> None:
        """Open the clicked/selected item -- a folder header does nothing."""
        is_remote = item.data(0, IS_REMOTE_ROLE)
        if is_remote is None:
            # A folder row: not a file at all, nothing to open.
            return
        data = item.data(0, PATH_ROLE)

        if not is_remote:
            local_path = data
            if os.path.isfile(local_path):
                self._dispatch_open(local_path)
                self.accept()
            else:
                QMessageBox.warning(self, "Open File", f"File not found on disk:\n{local_path}")
            return

        # Remote and not mirrored: opening it needs a download first, which
        # used to happen silently -- clicking Open did nothing at all, and
        # nothing on screen said why.
        answer = QMessageBox.question(
            self,
            "Not downloaded yet",
            f"'{data}' has not been downloaded.\n\nDownload it now and open it?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._download_and_open(item, data)

    def _download_and_open(self, item: QTreeWidgetItem, remote_name: str) -> None:
        self.btn_open.setEnabled(False)
        self.lbl_status.setText(f"Downloading {remote_name}...")
        job_id = self.job.id

        def on_ready(finished_id, paths) -> None:
            if finished_id != job_id:
                return
            self.service.results_ready.disconnect(on_ready)
            self.service.error.disconnect(on_error)
            self.btn_open.setEnabled(True)
            match = next((p for p in paths or [] if os.path.basename(p) == remote_name), None)
            if match is None:
                self.lbl_status.setText(f"Download finished, but {remote_name} was not in it.")
                return
            self._dispatch_open(match)
            self.accept()

        def on_error(msg: str) -> None:
            # download() reports failure job-wide, with no job id on the
            # signal -- the job's own name in the message is what ties it back.
            if self.job.name not in (msg or ""):
                return
            self.service.results_ready.disconnect(on_ready)
            self.service.error.disconnect(on_error)
            self.btn_open.setEnabled(True)
            self.lbl_status.setText(f"Could not download {remote_name}: {msg}")

        self.service.results_ready.connect(on_ready)
        self.service.error.connect(on_error)
        self.service.download(self.job, names=[remote_name])

    def _on_selection_changed(self) -> None:
        item = self.tree.currentItem()
        if item is not None and item.data(0, IS_REMOTE_ROLE) is not None:
            self.btn_open.setEnabled(True)
        else:
            self.btn_open.setEnabled(False)

    def _dispatch_open(self, path: str) -> None:
        """Hand the file path to the host opener callback or default open_in_host."""
        if self.on_open_callback is not None:
            self.on_open_callback(path)
            return

        from .jobs_dialog import open_in_host

        open_in_host(path)

    def _open_containing_folder(self) -> None:
        """Open the folder in system file manager."""
        target_dir = ""
        if self.job.local_dir and os.path.isdir(self.job.local_dir):
            target_dir = self.job.local_dir
        else:
            cache_dir = os.path.join(
                tempfile.gettempdir(), "moleditpy_job_manager_cache", self.job.id
            )
            if os.path.isdir(cache_dir):
                target_dir = cache_dir

        if target_dir and os.path.isdir(target_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target_dir))
        else:
            QMessageBox.information(
                self,
                "Open Folder",
                "No local directory has been created for this job yet.\n"
                "Open a file or download results first.",
            )


__all__ = ["OutputFileSelectorDialog", "describe_file_type", "format_file_size"]
