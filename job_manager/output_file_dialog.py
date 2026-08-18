"""Output file selector dialog.

Allows users to choose which output file from a job to open in MoleditPy.
Downloaded files and verified equal-path mirror files can be opened directly.
Displays all results in a hierarchical folder tree scoped strictly to the job.
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable, List, Optional, Sequence

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION
from .file_tree import FULL_HEADERS, configure_tree, folder_factory, make_filter_row
from .models import Job
from .theme import apply_theme
from .tree_utils import (
    IS_REMOTE_ROLE,
    PATH_ROLE,
    ensure_folder_item,
    filter_tree_items,
    split_path,
)

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


def _is_within(path: str, root: str) -> bool:
    """Return whether ``path`` is inside ``root`` on the current OS."""
    try:
        return os.path.commonpath(
            [os.path.normpath(path), os.path.normpath(root)]
        ) == os.path.normpath(root)
    except ValueError:
        return False


def _relative_name(path: str, root: str) -> str:
    """Return a normalized relative path, or an empty value across drives."""
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except (OSError, ValueError):
        return ""


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

        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Open Result: {job.name}")
        apply_theme(self)
        self.resize(650, 480)

        self._build_ui()
        self._load_files()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Headline, filter, tree, buttons -- the same order and the same
        # pieces as the download and tail-file lists.
        self.lbl_headline = QLabel(f"Select a file from <b>{self.job.name}</b> to open:")
        self.lbl_headline.setWordWrap(True)
        layout.addWidget(self.lbl_headline)

        filter_row, self.txt_filter = make_filter_row(self._apply_filter)
        layout.addLayout(filter_row)

        self.tree = configure_tree(QTreeWidget(), FULL_HEADERS)
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

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Close
        )
        self.btn_open = box.button(QDialogButtonBox.StandardButton.Open)
        self.btn_open.setDefault(True)
        self.btn_open.clicked.connect(self._open_selected)
        self.btn_close = box.button(QDialogButtonBox.StandardButton.Close)
        # The box emits rejected for a Close button; connecting clicked as well
        # would reject twice and emit finished twice.
        box.rejected.connect(self.reject)

        self.btn_open_folder = QPushButton("Open Containing Folder")
        self.btn_open_folder.setToolTip("Show the downloaded files in the file manager.")
        self.btn_open_folder.clicked.connect(self._open_containing_folder)
        box.addButton(self.btn_open_folder, QDialogButtonBox.ButtonRole.ActionRole)

        self.btn_browse_remote = QPushButton("Browse Host Files...")
        self.btn_browse_remote.setToolTip("List everything in the job's directory on the host.")
        self.btn_browse_remote.clicked.connect(self._fetch_remote_listing)
        box.addButton(self.btn_browse_remote, QDialogButtonBox.ButtonRole.ActionRole)

        layout.addWidget(box)

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
        """Find all local (or locally mirrored) output files associated strictly with this job."""
        files: List[str] = []
        for path in self.job.downloaded_files or []:
            if path and os.path.isfile(path) and path not in files:
                files.append(os.path.normpath(path))

        mirror_dir = self._mirrored_job_dir()
        if mirror_dir and os.path.isdir(mirror_dir):
            try:
                for root, _, entries in os.walk(mirror_dir):
                    for entry in entries:
                        if not entry.startswith("."):
                            full = os.path.normpath(os.path.join(root, entry))
                            if os.path.isfile(full) and full not in files:
                                files.append(full)
            except OSError:
                pass

        cache_dir = os.path.join(tempfile.gettempdir(), "moleditpy_job_manager_cache", self.job.id)
        if os.path.isdir(cache_dir):
            try:
                for root, _, entries in os.walk(cache_dir):
                    for entry in entries:
                        if not entry.startswith("."):
                            full = os.path.normpath(os.path.join(root, entry))
                            if os.path.isfile(full) and full not in files:
                                files.append(full)
            except OSError:
                pass

        # If job.local_dir is a dedicated job directory (under store.download_root), include its files.
        # If it's a shared working directory (download_beside_input), only downloaded_files are included.
        if self.job.local_dir and os.path.isdir(self.job.local_dir):
            dl_root = ""
            if hasattr(self.service, "store") and hasattr(self.service.store, "download_root"):
                try:
                    dl_root = self.service.store.download_root()
                except Exception:
                    dl_root = ""
            if dl_root and _is_within(self.job.local_dir, dl_root):
                try:
                    for root, _, entries in os.walk(self.job.local_dir):
                        for entry in entries:
                            if not entry.startswith("."):
                                full = os.path.normpath(os.path.join(root, entry))
                                if os.path.isfile(full) and full not in files:
                                    files.append(full)
                except OSError:
                    pass

        return files

    def _relative_local_path(self, path: str, mirror_dir: str, cache_dir: str) -> str:
        """Return a relative path suited for displaying in the tree hierarchy."""
        if mirror_dir and _is_within(path, mirror_dir):
            rel = _relative_name(path, mirror_dir)
            if rel:
                return rel
        if self.job.local_dir and _is_within(path, self.job.local_dir):
            rel = _relative_name(path, self.job.local_dir)
            if rel:
                return rel
        if cache_dir and _is_within(path, cache_dir):
            rel = _relative_name(path, cache_dir)
            if rel:
                return rel
        return os.path.basename(path)

    def _populate_tree_local(self, paths: Sequence[str]) -> None:
        """Fill tree with local and mirrored files structured as a folder hierarchy."""
        self.tree.clear()
        self._all_items.clear()

        from .runner import primary_output

        # Never the wrapper's own job.log: it is this plugin's file, and
        # opening it is not what "open the result" means.
        primary_path = primary_output(list(paths), self.job.log_file)
        selected_item: Optional[QTreeWidgetItem] = None
        mirror_dir = self._mirrored_job_dir()
        norm_mirror = os.path.normpath(mirror_dir) if mirror_dir else ""
        cache_dir = os.path.join(tempfile.gettempdir(), "moleditpy_job_manager_cache", self.job.id)
        folders: dict = {}

        for path in paths:
            rel_name = self._relative_local_path(path, norm_mirror, cache_dir)
            parts = split_path(rel_name)
            if not parts:
                continue

            parent = ensure_folder_item(
                self.tree, folders, parts[:-1], folder_factory(columns=len(FULL_HEADERS))
            )
            filename = parts[-1]

            try:
                size_str = format_file_size(os.path.getsize(path))
            except OSError:
                size_str = "-"
            type_str = describe_file_type(filename)
            if norm_mirror and _is_within(path, norm_mirror):
                location = "Mirror (no download needed)"
                tooltip = f"Opened directly from the host's local mirror:\n{path}"
            else:
                location = "Local"
                tooltip = path

            item = QTreeWidgetItem([filename, size_str, type_str, location])
            item.setData(0, PATH_ROLE, path)
            item.setData(0, IS_REMOTE_ROLE, False)
            item.setToolTip(0, tooltip)

            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            self._all_items.append(item)

            if path == primary_path:
                selected_item = item

        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        elif self._all_items:
            self.tree.setCurrentItem(self._all_items[0])

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
        """Return the verified equal-path directory for this job, if any."""
        from .models import HostProfile

        host = self.service.store.hosts.get(self.job.host_id)
        if not isinstance(host, HostProfile):
            return ""
        try:
            return host.mirrored_job_dir(self.job.remote_dir)
        except (OSError, TypeError, ValueError):
            return ""

    def _populate_tree_remote(self, names: Sequence[str]) -> None:
        """Populate the tree with remote files under the host's folder structure,
        merging in any already-downloaded local files."""
        self.tree.clear()
        self._all_items.clear()

        local_files = self._get_existing_local_files()
        local_map = {}
        for path in local_files:
            if self.job.local_dir and _is_within(path, self.job.local_dir):
                relative = _relative_name(path, self.job.local_dir)
            else:
                relative = os.path.basename(path)
            if relative:
                local_map[relative] = path
        mirror_dir = self._mirrored_job_dir()

        from .runner import primary_output

        primary_name = os.path.basename(primary_output(list(names), self.job.log_file))
        selected_item: Optional[QTreeWidgetItem] = None
        folders: dict = {}
        gray = QColor("#8b949e")

        for name in names:
            if not name or name.startswith("."):
                continue
            parts = split_path(name)
            if not parts:
                continue
            filename = parts[-1]
            parent = ensure_folder_item(
                self.tree, folders, parts[:-1], folder_factory(columns=len(FULL_HEADERS))
            )

            mirrored_path = os.path.join(mirror_dir, *parts) if mirror_dir else ""
            local_key = "/".join(parts)
            local_path = local_map.get(local_key)
            if local_path is None and len(parts) == 1:
                local_path = local_map.get(filename)
            if local_path is not None:
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
        """Filter tree rows based on search input while preserving folder hierarchy."""
        filter_tree_items(self.tree, text)

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
            wanted = remote_name.replace("\\", "/").strip("/")
            local_root = self.job.local_dir or ""
            match = next(
                (
                    p
                    for p in paths or []
                    if (
                        (
                            _relative_name(p, local_root) == wanted
                            if local_root
                            else os.path.basename(p) == wanted
                        )
                        or ("/" not in wanted and os.path.basename(p) == wanted)
                    )
                ),
                None,
            )
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
        mirror_dir = self._mirrored_job_dir()
        if mirror_dir and os.path.isdir(mirror_dir):
            target_dir = mirror_dir
        elif self.job.local_dir and os.path.isdir(self.job.local_dir):
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
