"""A permanent job counter in the main window's status bar.

The plugin used to have no presence in the application at all: unless the
monitor was open, a user had no way of knowing whether anything was running,
and after a restart nothing was even being polled. This widget is the standing
answer to "is the cluster still busy?", and creating it is also what starts
background tracking for jobs left over from a previous session.

The host app already puts its formula label in the status bar with
``addPermanentWidget``, so this needs no new API on the host side.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel

from . import taskbar
from .models import STATE_RUNNING
from .service import JobService

#: Colours match the monitor's State column, so the two agree at a glance.
_BUSY_COLOR = "#2e7d32"
_BLOCKED_COLOR = "#c62828"


class JobStatusWidget(QLabel):
    """One line of text: how many jobs are running, waiting, or stuck."""

    def __init__(self, service: JobService, on_click=None, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Named methods, not lambdas: a lambda slot keeps a strong reference to
        # this widget and can never be disconnected again, so a status bar
        # rebuilt by the host would leave the old one updating for ever.
        self._connections = [
            (service.jobs_changed, self.refresh),
            (service.job_updated, self._on_job_updated),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)
        self.refresh()

    def _on_job_updated(self, _job_id: str = "") -> None:
        self.refresh()

    def counts(self) -> dict:
        """Running / waiting / blocked, counted off the live store."""
        store = self.service.store
        running = waiting = blocked = 0
        for job in store.active_jobs():
            if store.chain_blocker(job) is not None:
                blocked += 1
            elif job.state == STATE_RUNNING:
                running += 1
            else:
                waiting += 1
        return {"running": running, "waiting": waiting, "blocked": blocked}

    def summary(self, counts: Optional[dict] = None) -> str:
        """The text shown, or "" when there is nothing to report."""
        counts = self.counts() if counts is None else counts
        parts = []
        if counts["running"]:
            parts.append(f"{counts['running']} running")
        if counts["waiting"]:
            parts.append(f"{counts['waiting']} queued")
        if counts["blocked"]:
            parts.append(f"{counts['blocked']} blocked")
        return "  ".join(parts)

    def refresh(self) -> None:
        # Counted once: this runs on every job update, and each pass walks the
        # whole job list asking the store about every chain.
        counts = self.counts()
        text = self.summary(counts)
        # The OS task bar / Dock badge carries the same count, so a minimised
        # MoleditPy still says the cluster is busy -- but only if asked. The
        # application icon belongs to the host, not to a plugin.
        if self.service.store.get_pref("taskbar_badge", False):
            taskbar.set_badge(sum(counts.values()))
        # Hidden rather than empty: an always-present blank label steals status
        # bar width from the host for a plugin the user may never use.
        self.setVisible(bool(text))
        if not text:
            self.setText("")
            return
        color = _BLOCKED_COLOR if counts["blocked"] else _BUSY_COLOR
        self.setText(f"⚙ {text}")
        self.setStyleSheet(f"color: {color};")
        self.setToolTip(
            "MoleditPy job manager: click to open the monitor.\n"
            + (
                f"{counts['blocked']} job(s) are waiting for something that failed "
                "and will never start."
                if counts["blocked"]
                else "Jobs are being tracked in the background."
            )
        )

    def mouseDoubleClickEvent(self, event) -> None:
        """Same as a single click: a counter that opens on one is expected to
        open on two, and Qt sends press, release, then double-click."""
        if self._on_click is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._on_click is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mouseReleaseEvent(event)

    def detach(self) -> None:
        """Undo every connection, clear the badge, and leave the status bar."""
        taskbar.clear_badge()
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except TypeError:
                logging.debug("Job Manager: status widget already disconnected")
        self._connections = []
        parent = self.parent()
        if parent is not None and hasattr(parent, "removeWidget"):
            try:
                parent.removeWidget(self)
            except Exception:
                logging.debug("Job Manager: status widget not removed", exc_info=True)
        self.setParent(None)


def install(main_window, service: JobService, on_click=None) -> Optional[JobStatusWidget]:
    """Add the counter to ``main_window``'s status bar. None if there is none."""
    status_bar = main_window.statusBar() if hasattr(main_window, "statusBar") else None
    if status_bar is None:
        return None
    widget = JobStatusWidget(service, on_click=on_click)
    status_bar.addPermanentWidget(widget)
    return widget
