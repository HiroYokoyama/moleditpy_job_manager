"""How the plugin shows itself outside its own windows.

The status bar counter used to be the only standing sign of a busy cluster,
and it is invisible the moment MoleditPy is minimised. This keeps the tray
icon, the task bar button and the window title saying the same thing, and
flags a job ending on the task bar when nobody is looking at the application.

The counting and the choice of what to show are plain functions so they are
tested without a desktop; :class:`Presence` only pushes their answers out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from PyQt6.QtCore import QObject

from .models import STATE_FAILED, STATE_LOST, STATE_RUNNING

#: Ending states that want the user's attention rather than just a note.
FAILURE_STATES = (STATE_FAILED, STATE_LOST)


def count_jobs(store) -> dict:
    """Running / waiting / blocked, counted off the live store."""
    running = waiting = blocked = 0
    blocked_ids = store.blocked_ids()
    for job in store.active_jobs():
        if job.id in blocked_ids:
            blocked += 1
        elif job.state == STATE_RUNNING:
            running += 1
        else:
            waiting += 1
    return {"running": running, "waiting": waiting, "blocked": blocked}


def summary_text(counts: dict, separator: str = "  ") -> str:
    """ "2 running  1 queued", or "" when there is nothing to report."""
    parts = []
    if counts.get("running"):
        parts.append(f"{counts['running']} running")
    if counts.get("waiting"):
        parts.append(f"{counts['waiting']} queued")
    if counts.get("blocked"):
        parts.append(f"{counts['blocked']} blocked")
    return separator.join(parts)


def title_prefix(counts: dict) -> str:
    """Put ahead of a window title, which the task bar truncates from the end."""
    text = summary_text(counts, ", ")
    return f"{text} - " if text else ""


@dataclass(frozen=True)
class Progress:
    """What a task bar button's progress bar shows."""

    state: str = "none"  # a win_taskbar.PROGRESS_FLAGS key
    value: int = 0
    total: int = 0


def taskbar_progress(counts: dict, batch_total: int, unseen_failures: int) -> Progress:
    """Turn the job counts into a progress bar.

    ``batch_total`` is every job seen active since the list was last idle, so
    the bar fills as a batch of five works through: 3 of 5 done is 60 %. A
    single job, or a batch where nothing has ended yet, has no fraction worth
    drawing and pulses instead. Red for a failure nobody has looked at yet
    beats yellow for a blocked chain, which beats green.
    """
    active = sum(counts.values())
    if active == 0:
        return Progress("error", 1, 1) if unseen_failures else Progress()
    total = max(batch_total, active)
    done = total - active
    if unseen_failures:
        state = "error"
    elif counts.get("blocked"):
        state = "paused"
    elif done == 0:
        return Progress("indeterminate")
    else:
        state = "normal"
    # A red or yellow bar with nothing filled in is an empty bar: show it full.
    return Progress(state, done or total, total)


def tray_state(counts: dict, unseen_failures: int) -> str:
    """The dot on the tray icon: "error", "busy", "queued" or "idle"."""
    if unseen_failures or counts.get("blocked"):
        return "error"
    if counts.get("running"):
        return "busy"
    if counts.get("waiting"):
        return "queued"
    return "idle"


class Presence(QObject):
    """Keeps the tray, the task bar and the job monitor's title current."""

    def __init__(self, service, main_window=None, actions: Optional[dict] = None) -> None:
        super().__init__()
        self.service = service
        self.main_window = main_window
        self.unseen_failures = 0
        self._batch: set = set()
        self._windows: List[object] = []
        self._title_listeners: List[Callable[[dict], None]] = []
        self._main_taskbar = None
        self.tray = None
        self._connections = [
            (service.jobs_changed, self.refresh),
            (service.job_updated, self._on_job_updated),
            (service.job_finished, self._on_job_finished),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)
        try:
            from .tray import TrayController

            self.tray = TrayController(service, self, main_window, actions or {})
            self.tray.install()
        except Exception:
            logging.debug("Job Manager: no tray icon", exc_info=True)
            self.tray = None
        self.refresh()

    # --- state ---------------------------------------------------------------

    def counts(self) -> dict:
        return count_jobs(self.service.store)

    def acknowledge(self) -> None:
        """The user has looked: a failure stops colouring the icons red."""
        if self.unseen_failures:
            self.unseen_failures = 0
            self.refresh()

    def _on_job_updated(self, _job_id: str = "") -> None:
        self.refresh()

    def _on_job_finished(self, _job_id: str, state: str) -> None:
        if state in FAILURE_STATES:
            self.unseen_failures += 1
        self.refresh()
        if self.service.store.get_pref("flash_on_finish", True):
            self.alert()

    def alert(self) -> None:
        """Flash the task bar button (bounce the Dock icon) until looked at.

        At the job monitor when it is open, since that is where the answer is;
        otherwise at MoleditPy. Qt makes this a no-op for the active window.
        """
        from PyQt6.QtWidgets import QApplication

        target = None
        for window in self._windows:
            widget = getattr(window, "widget", None)
            if widget is not None and widget.isVisible():
                target = widget
                break
        if target is None and self.main_window is not None:
            try:
                if self.main_window.isVisible():
                    target = self.main_window
            except RuntimeError:
                target = None
        if target is None:
            return
        try:
            QApplication.alert(target, 0)
        except Exception:
            logging.debug("Job Manager: the task bar was not flashed", exc_info=True)

    # --- what shows it -------------------------------------------------------

    def add_window(self, window_taskbar) -> None:
        """A plugin window's task bar button, to carry the progress bar too."""
        if window_taskbar not in self._windows:
            self._windows.append(window_taskbar)
        self.refresh()

    def remove_window(self, window_taskbar) -> None:
        if window_taskbar in self._windows:
            self._windows.remove(window_taskbar)
            try:
                window_taskbar.clear()
            except Exception:
                logging.debug("Job Manager: window progress not cleared", exc_info=True)

    def add_title_listener(self, listener: Callable[[dict], None]) -> None:
        self._title_listeners.append(listener)

    def remove_title_listener(self, listener: Callable[[dict], None]) -> None:
        if listener in self._title_listeners:
            self._title_listeners.remove(listener)

    def refresh(self) -> None:
        store = self.service.store
        counts = self.counts()
        active_ids = {job.id for job in store.active_jobs()}
        if active_ids:
            self._batch |= active_ids
        else:
            self._batch.clear()
        progress = taskbar_progress(counts, len(self._batch), self.unseen_failures)

        if store.get_pref("taskbar_progress", True):
            for window in list(self._windows):
                window.set_progress(progress.state, progress.value, progress.total)
        else:
            for window in list(self._windows):
                window.clear()
        self._update_main_taskbar(progress)

        for listener in list(self._title_listeners):
            try:
                listener(counts)
            except Exception:
                logging.debug("Job Manager: a title listener failed", exc_info=True)

        if self.tray is not None:
            self.tray.update(counts, self.unseen_failures)

    def _update_main_taskbar(self, progress: Progress) -> None:
        """MoleditPy's own button, only on the same opt-in as the badge."""
        from . import win_taskbar

        if not win_taskbar.AVAILABLE or self.main_window is None:
            return
        wanted = bool(self.service.store.get_pref("taskbar_badge", False)) and bool(
            self.service.store.get_pref("taskbar_progress", True)
        )
        if self._main_taskbar is None:
            if not wanted:
                return
            self._main_taskbar = win_taskbar.WindowTaskbar(self.main_window)
        if wanted:
            self._main_taskbar.set_progress(progress.state, progress.value, progress.total)
        else:
            self._main_taskbar.clear()

    def detach(self) -> None:
        """Undo every connection and take everything shown back down."""
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except TypeError:
                logging.debug("Job Manager: presence already disconnected")
        self._connections = []
        for window in list(self._windows):
            self.remove_window(window)
        if self._main_taskbar is not None:
            try:
                self._main_taskbar.clear()
            except Exception:
                logging.debug("Job Manager: main progress not cleared", exc_info=True)
            self._main_taskbar = None
        self._title_listeners = []
        if self.tray is not None:
            try:
                self.tray.detach()
            except Exception:
                logging.debug("Job Manager: tray teardown failed", exc_info=True)
            self.tray = None


_current: Optional[Presence] = None


def current() -> Optional[Presence]:
    """The live presence, if the plugin has started tracking."""
    return _current


def install(service, main_window=None, actions: Optional[dict] = None) -> Presence:
    global _current
    if _current is not None:
        _current.detach()
    _current = Presence(service, main_window, actions)
    return _current


def uninstall() -> None:
    global _current
    if _current is not None:
        _current.detach()
        _current = None


__all__ = [
    "Presence",
    "Progress",
    "count_jobs",
    "current",
    "install",
    "summary_text",
    "taskbar_progress",
    "title_prefix",
    "tray_state",
    "uninstall",
]
