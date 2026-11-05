"""The job file browser, pointed at a host's whole remote root.

The job dialogs only reach the files of one job. What a queue left beside the
jobs, or a job the list no longer knows about, could only be found in a terminal.

A remote root can hold millions of files, so a folder is listed only when it is
opened. Listing the whole root up front kept the window waiting on the host and
then on building the tree.
"""

from __future__ import annotations

import os
from html import escape
from typing import Callable, Dict, Iterable, Optional, Set

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTreeWidgetItem,
    QWidget,
)

from . import PLUGIN_VERSION
from .file_tree import FULL_HEADERS, folder_factory
from .loading_dialog import close_loading_dialog, open_loading_dialog
from .models import HostProfile, Job
from .output_file_dialog import OutputFileSelectorDialog, format_file_size
from .remote_paths import join as remote_join
from .result_files import safe_relative_name
from .runner import effective_root
from .tree_utils import IS_REMOTE_ROLE, PATH_ROLE, split_path

#: The most rows shown for one folder. A folder holding far more would stall the
#: window while its rows are built, and the status line says when it stops.
MAX_LISTED_FILES = 50_000

#: A file that has to be fetched whole gets a question above this size.
LARGE_FILE_BYTES = 100 * 1024 * 1024

#: The folder a row stands for, relative to the remote root.
_FOLDER_ROLE = Qt.ItemDataRole.UserRole + 2

_ROOT = ""


def browsable_hosts(service) -> list:
    """Shell hosts and authenticated Job Server virtual job folders."""
    return list(service.store.hosts.values())


class HostFileBrowserDialog(OutputFileSelectorDialog):
    """Lists the remote root a folder at a time, and always reads the host's copy."""

    def __init__(self, service, host: HostProfile, parent: Optional[QWidget] = None, **callbacks):
        self.host = host
        #: Folder rows by relative path.
        self._folders: Dict[str, QTreeWidgetItem] = {}
        self._listed: Set[str] = set()
        self._listing: Set[str] = set()
        #: Open folders to open again as Refresh reaches them.
        self._reopen: Set[str] = set()
        #: Replies carrying an older number were cancelled, or belong to a tree
        #: Refresh has replaced: the worker cannot be stopped, only ignored.
        self._listing_round = 0
        self._fetch_round = 0
        self._loading: Optional[QProgressDialog] = None
        root_as_job = Job(
            id=f"host-files-{host.id}",
            name=host.name,
            host_id=host.id,
            host_name=host.name,
            remote_dir="/" if host.is_job_server else effective_root(host),
        )
        super().__init__(service, root_as_job, parent, **callbacks)
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - File Browser: {host.name}")
        self.lbl_headline.setText(
            f"Files under <b>{escape(self.job.remote_dir)}</b> on <b>{escape(host.name)}</b>. "
            "A folder is read from the host when you open it. "
            "Double-click a file to view its text."
        )
        if host.is_job_server:
            self.lbl_headline.setText(
                f"Job folders on <b>{escape(host.name)}</b> (Job Server 0.7 or newer). "
                "Only jobs you may access are shown. Open a folder to list its files."
            )
        self.txt_filter.setPlaceholderText("Filter the folders opened so far...")
        self.btn_browse_remote.setText("Refresh")
        self.btn_browse_remote.setToolTip("Read the remote root and the open folders again.")
        # There is no local folder of these files to show.
        self.btn_open_folder.parentWidget().removeButton(self.btn_open_folder)
        self.tree.itemExpanded.connect(self._on_expanded)
        if host.is_job_server:
            self.btn_download = QPushButton("Download...")
            self.btn_download.clicked.connect(self._download_selected)
            self.btn_open.parentWidget().addButton(
                self.btn_download, QDialogButtonBox.ButtonRole.ActionRole
            )

    def _download_selected(self):
        if self._pending or self._listing:
            return
        paths = [
            item.data(0, _FOLDER_ROLE) or item.data(0, PATH_ROLE)
            for item in self.tree.selectedItems()
        ]
        if not paths:
            self.lbl_status.setText("Select a file or job folder to download.")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Download into", self.service.store.download_root()
        )
        if not folder:
            return
        self._pending = [("download", path) for path in paths]
        self.btn_download.setEnabled(False)

        def done(result):
            downloaded, skipped, _folder = result
            self._pending = []
            self.btn_download.setEnabled(True)
            self.lbl_status.setText(
                f"Downloaded {len(downloaded)} file(s); skipped {len(skipped)}."
            )

        def failed(message):
            self._pending = []
            self.btn_download.setEnabled(True)
            self.lbl_status.setText(f"Download failed: {message}")

        self.service.download_host_paths(
            self.host,
            ["/" + path for path in paths],
            folder,
            done,
            failed,
            overwrite=False,
            owner=self,
        )

    # --- the loading window ---------------------------------------------------

    def _show_loading(self, text: str, on_cancel: Callable[[], None]) -> None:
        if self._loading is None:
            self._loading = open_loading_dialog(self, "File Browser", text, on_cancel)
        else:
            self._loading.setLabelText(text)

    def _hide_loading(self) -> None:
        if self._loading is not None:
            close_loading_dialog(self._loading)
            self._loading = None

    # --- listing --------------------------------------------------------------

    def _load_files(self) -> None:
        self._fetch_remote_listing()

    def _fetch_remote_listing(self) -> None:
        """Start again from the root, keeping the open folders open."""
        self._reopen = {path for path, item in self._folders.items() if item.isExpanded()}
        self._listing_round += 1
        self.tree.clear()
        self._all_items.clear()
        self._folders.clear()
        self._listed.clear()
        self._listing.clear()
        self._list_folder(_ROOT)

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, _FOLDER_ROLE)
        if path is None or path in self._listed:
            return
        if self._pending:
            # A file is on its way, in the moment before its window appears.
            item.setExpanded(False)
            return
        self._list_folder(path)

    def _list_folder(self, path: str) -> None:
        if path in self._listing:
            return
        self._listing.add(path)
        if path == _ROOT:
            self.btn_browse_remote.setEnabled(False)
        where = path or "the remote root"
        self.lbl_status.setText(f"Listing {where}...")
        self._show_loading(f"Reading {where} from {self.host.name}...", self._cancel_listing)
        listing_round = self._listing_round

        def on_ok(names: list) -> None:
            if listing_round == self._listing_round:
                self._listing_ended(path)
                self._add_listing(path, names)
                self._hide_loading_if_idle()

        def on_error(message: str) -> None:
            if listing_round == self._listing_round:
                self._listing_ended(path)
                self.lbl_status.setText(f"Could not list {where}: {message}")
                self._close_folder(path)
                self._hide_loading_if_idle()

        remote = remote_join(self.job.remote_dir, path) if path else self.job.remote_dir
        self.service.list_host_path(self.host, remote, 1, on_ok, on_error, owner=self)

    def _listing_ended(self, path: str) -> None:
        self._listing.discard(path)
        if path == _ROOT:
            self.btn_browse_remote.setEnabled(True)

    def _hide_loading_if_idle(self) -> None:
        if not self._listing:
            self._hide_loading()

    def _close_folder(self, path: str) -> None:
        """Closed again, so that opening it once more is what tries again."""
        folder = self._folders.get(path)
        if folder is not None:
            folder.setExpanded(False)

    def _cancel_listing(self) -> None:
        self._hide_loading()
        self._listing_round += 1
        self._reopen.clear()
        self.btn_browse_remote.setEnabled(True)
        waiting = list(self._listing)
        self._listing.clear()
        for path in waiting:
            self._close_folder(path)
        self.lbl_status.setText("Cancelled.")

    def _add_listing(self, path: str, names: Iterable[str]) -> None:
        self._listed.add(path)
        # Hidden names are left out on every host: `ls -1` does not print them.
        visible = [name for name in names if name and not name.startswith(".")]
        # Folders first, as a file manager shows them.
        visible.sort(key=lambda name: (not name.endswith("/"), name.lower()))
        shown = visible[:MAX_LISTED_FILES]
        mirror_dir = self._mirrored_job_dir()
        rows = [self._row(path, name, mirror_dir) for name in shown]
        self._all_items.extend(row for row in rows if row.data(0, _FOLDER_ROLE) is None)

        parent = self._folders.get(path)
        if parent is None:
            self.tree.addTopLevelItems(rows)
        else:
            parent.addChildren(rows)
            if not rows:
                parent.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
                )

        where = path or "the remote root"
        if len(visible) > len(shown):
            self.lbl_status.setText(
                f"Showing the first {len(shown):,} of {len(visible):,} items in {where}."
            )
        else:
            self.lbl_status.setText(f"{len(shown):,} item(s) in {where}.")
        self._apply_filter(self.txt_filter.text())

        for row in rows:
            folder = row.data(0, _FOLDER_ROLE)
            if folder in self._reopen:
                self._reopen.discard(folder)
                row.setExpanded(True)

    def _row(self, folder: str, name: str, mirror_dir: str) -> QTreeWidgetItem:
        relative = f"{folder}/{name}" if folder else name
        parts = split_path(relative)
        if not name.endswith("/"):
            return self._remote_file_row(relative, parts, {}, mirror_dir)
        row = folder_factory(columns=len(FULL_HEADERS))(parts[-1], parts, None)
        if self.host.is_job_server:
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsSelectable)
        path = "/".join(parts)
        row.setData(0, _FOLDER_ROLE, path)
        # What is inside is not known yet, and the arrow is how it gets asked for.
        row.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self._folders[path] = row
        return row

    # --- opening --------------------------------------------------------------

    def _open_item(self, item: QTreeWidgetItem, as_text: bool = False) -> None:
        if item.data(0, IS_REMOTE_ROLE):
            self._download_and_open(item, item.data(0, PATH_ROLE), as_text)
        else:
            super()._open_item(item, as_text)

    def _download_and_open(
        self, item: QTreeWidgetItem, remote_name: str, as_text: bool = False
    ) -> None:
        # A folder may still be listing, in the moment before its window appears.
        if self._pending or self._listing:
            return
        safe_name = safe_relative_name(remote_name)
        if not safe_name:
            self.lbl_status.setText(f"Unsafe remote filename: {remote_name}")
            return
        self._pending = [("fetch", remote_name)]
        self.btn_open.setEnabled(False)
        self.lbl_status.setText(f"Checking {remote_name}...")
        self._show_loading(f"Checking {remote_name}...", self._cancel_fetch)
        remote = remote_join(self.job.remote_dir, remote_name)
        fetch_round = self._fetch_round

        def current() -> bool:
            return fetch_round == self._fetch_round

        def checked(info: dict) -> None:
            if not current():
                return
            # A link to a folder is listed without the mark a folder has.
            if info.get("type") == "directory":
                self._fetch_ended(f"{remote_name} is a link to a folder, and cannot be opened.")
                return
            size = info.get("size") or 0
            if size > LARGE_FILE_BYTES:
                # Its own question: a cancel button beside it would answer it twice.
                self._hide_loading()
                if not self._confirm_large(remote_name, size):
                    self._fetch_ended("")
                    return
            self.lbl_status.setText(f"Downloading {remote_name}...")
            self._show_loading(f"Downloading {remote_name}...", self._cancel_fetch)
            # Under its own path: every job directory holds a file called the same.
            folder = os.path.join(
                self.service.store.cache_dir(self.job.id, create=True),
                *os.path.dirname(safe_name).split("/"),
            )
            self.service.download_host_paths(
                self.host, [remote], folder, done, failed, overwrite=True, owner=self
            )

        def done(result: tuple) -> None:
            if not current():
                return
            downloaded, skipped, _folder = result
            if not downloaded:
                failed(skipped[0][1] if skipped else "the transfer failed")
                return
            self._fetch_ended(f"Downloaded {remote_name}.")
            if self._dispatch_open(downloaded[0], as_text=as_text) and not as_text:
                self.accept()

        def failed(message: str) -> None:
            if current():
                self._fetch_ended(f"Could not download {remote_name}: {message}")

        self.service.stat_host_path(self.host, remote, False, checked, failed, owner=self)

    def _cancel_fetch(self) -> None:
        self._fetch_ended("Cancelled.")

    def _fetch_ended(self, status: str) -> None:
        self._fetch_round += 1
        self._hide_loading()
        self._pending = []
        self.btn_open.setEnabled(True)
        self.lbl_status.setText(status)

    def _disconnect_download(self) -> None:
        # Nothing is connected to the service: replies go through ``owner``.
        # Closing the window is what brings this here, so nothing is awaited now.
        self._fetch_round += 1
        self._listing_round += 1
        self._hide_loading()
        self._pending = []

    def _confirm_large(self, name: str, size: int) -> bool:
        answer = QMessageBox.question(
            self,
            "Large file",
            f"{name} is {format_file_size(size)}, and all of it has to be fetched first.\n\n"
            "Fetch it?",
        )
        return answer == QMessageBox.StandardButton.Yes


__all__ = ["HostFileBrowserDialog", "LARGE_FILE_BYTES", "MAX_LISTED_FILES", "browsable_hosts"]
