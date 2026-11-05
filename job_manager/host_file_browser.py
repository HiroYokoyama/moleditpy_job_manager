"""The job file browser, pointed at a host's whole remote root.

The job dialogs only reach the files of one job. What a queue left beside the
jobs, or a job the list no longer knows about, could only be found in a terminal.
"""

from __future__ import annotations

import os
from html import escape
from typing import Optional, Sequence

from PySide6.QtWidgets import QMessageBox, QTreeWidgetItem, QWidget

from . import PLUGIN_VERSION
from .models import HostProfile, Job
from .output_file_dialog import OutputFileSelectorDialog, format_file_size
from .remote_paths import join as remote_join
from .result_files import safe_relative_name
from .runner import effective_root
from .tree_utils import IS_REMOTE_ROLE, PATH_ROLE

#: The most rows shown. A root holding far more would stall the window while the
#: tree is built, and the status line says when it stops.
MAX_LISTED_FILES = 50_000

#: A file that has to be fetched whole gets a question above this size.
LARGE_FILE_BYTES = 100 * 1024 * 1024


def browsable_hosts(service) -> list:
    """Hosts with a shell to list files through: a Job Server has none."""
    return [host for host in service.store.hosts.values() if not host.is_job_server]


class HostFileBrowserDialog(OutputFileSelectorDialog):
    """Lists from the remote root at any depth, and always reads the host's copy."""

    def __init__(self, service, host: HostProfile, parent: Optional[QWidget] = None, **callbacks):
        self.host = host
        root_as_job = Job(
            id=f"host-files-{host.id}",
            name=host.name,
            host_id=host.id,
            host_name=host.name,
            remote_dir=effective_root(host),
        )
        super().__init__(service, root_as_job, parent, **callbacks)
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - File Browser: {host.name}")
        self.lbl_headline.setText(
            f"Files under <b>{escape(self.job.remote_dir)}</b> on <b>{escape(host.name)}</b>. "
            "Double-click a file to view its text."
        )
        self.btn_browse_remote.setText("Refresh")
        self.btn_browse_remote.setToolTip("List the remote root again.")
        # There is no local folder of these files to show.
        self.btn_open_folder.parentWidget().removeButton(self.btn_open_folder)

    def _load_files(self) -> None:
        self._fetch_remote_listing()

    def _fetch_remote_listing(self) -> None:
        self.lbl_status.setText("Listing files on host...")
        self.btn_browse_remote.setEnabled(False)

        def on_ok(names: list) -> None:
            self.btn_browse_remote.setEnabled(True)
            self._populate_tree_remote(names)

        def on_error(message: str) -> None:
            self.btn_browse_remote.setEnabled(True)
            self.lbl_status.setText(f"Could not list remote files: {message}")

        self.service.list_host_files(self.host, on_ok, on_error, owner=self)

    def _cache_dir(self) -> str:
        # No cached copies in the listing: a file here may still be being written,
        # so every open reads it from the host again.
        return ""

    def _populate_tree_remote(self, names: Sequence[str]) -> None:
        names = sorted(names)
        shown = names[:MAX_LISTED_FILES]
        # Painting after every row of a large listing takes minutes.
        self.tree.setUpdatesEnabled(False)
        try:
            super()._populate_tree_remote(shown)
            self.tree.collapseAll()
        finally:
            self.tree.setUpdatesEnabled(True)
        if len(names) > len(shown):
            self.lbl_status.setText(f"Showing the first {len(shown):,} of {len(names):,} files.")

    def _open_item(self, item: QTreeWidgetItem, as_text: bool = False) -> None:
        if item.data(0, IS_REMOTE_ROLE):
            self._download_and_open(item, item.data(0, PATH_ROLE), as_text)
        else:
            super()._open_item(item, as_text)

    def _download_and_open(
        self, item: QTreeWidgetItem, remote_name: str, as_text: bool = False
    ) -> None:
        if self._pending:
            return
        safe_name = safe_relative_name(remote_name)
        if not safe_name:
            self.lbl_status.setText(f"Unsafe remote filename: {remote_name}")
            return
        self._pending = [("fetch", remote_name)]
        self.btn_open.setEnabled(False)
        self.lbl_status.setText(f"Checking {remote_name}...")
        remote = remote_join(self.job.remote_dir, remote_name)

        def checked(info: dict) -> None:
            size = info.get("size") or 0
            if size > LARGE_FILE_BYTES and not self._confirm_large(remote_name, size):
                self._fetch_ended("")
                return
            self.lbl_status.setText(f"Downloading {remote_name}...")
            # Under its own path: every job directory holds a file called the same.
            folder = os.path.join(
                self.service.store.cache_dir(self.job.id, create=True),
                *os.path.dirname(safe_name).split("/"),
            )
            self.service.download_host_paths(
                self.host, [remote], folder, done, failed, overwrite=True, owner=self
            )

        def done(result: tuple) -> None:
            downloaded, skipped, _folder = result
            if not downloaded:
                failed(skipped[0][1] if skipped else "the transfer failed")
                return
            self._fetch_ended(f"Downloaded {remote_name}.")
            if self._dispatch_open(downloaded[0], as_text=as_text) and not as_text:
                self.accept()

        def failed(message: str) -> None:
            self._fetch_ended(f"Could not download {remote_name}: {message}")

        self.service.stat_host_path(self.host, remote, False, checked, failed, owner=self)

    def _fetch_ended(self, status: str) -> None:
        self._pending = []
        self.btn_open.setEnabled(True)
        self.lbl_status.setText(status)

    def _disconnect_download(self) -> None:
        # Nothing is connected to the service: replies go through ``owner``.
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
