"""Shared pieces for the API tests: a service that never leaves the machine,
and a way to make a blocking client call while Qt's event loop runs.

Qt is imported inside :func:`in_thread` rather than at module level, because
:mod:`tests.test_api_core` imports this file and runs on the CI job that
installs only pytest.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, List, Optional

from job_manager.models import Job


class FakeSignal:
    """Just enough of a pyqtSignal for the handler paths that use one."""

    def __init__(self) -> None:
        self.slots: List[Callable] = []

    def connect(self, slot: Callable) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: Callable) -> None:
        if slot not in self.slots:
            raise TypeError("not connected")
        self.slots.remove(slot)

    def emit(self, *args: Any) -> None:
        for slot in list(self.slots):
            slot(*args)


class FakeService:
    """A JobService that records calls instead of touching a host."""

    def __init__(self, job_store: Any) -> None:
        self.store = job_store
        self.results_ready = FakeSignal()
        self.error = FakeSignal()
        self.submitted: List[tuple] = []
        self.cancelled: List[tuple] = []
        self.downloads: List[tuple] = []
        self.tails: List[tuple] = []
        self.listings: List[str] = []
        self.removed: List[str] = []
        self.download_returns = True
        self._tail_done: Optional[Callable] = None
        self._tail_error: Optional[Callable] = None
        self._list_ok: Optional[Callable] = None
        self._list_error: Optional[Callable] = None

    def submit(self, host, preset, name, files, **kwargs):
        job = Job(
            name=name or (os.path.basename(files[0]) if files else "job"),
            host_id=host.id,
            host_name=host.name,
            scheduler=host.scheduler,
            input_files=list(files),
            command=preset.command_template,
            preset=preset.to_dict(),
        )
        self.submitted.append((host, preset, name, list(files), dict(kwargs)))
        self.store.add_job(job)
        return job

    def cancel(self, job, release_dependents=True):
        self.cancelled.append((job.id, release_dependents))

    def download(self, job, into="", names=None):
        self.downloads.append((job.id, into, list(names) if names else None))
        return self.download_returns

    def tail_file(self, job, filename, lines=200, on_done=None, on_error=None):
        self.tails.append((job.id, filename, lines))
        self._tail_done = on_done
        self._tail_error = on_error

    def list_remote_results(self, job, on_ok, on_error):
        self.listings.append(job.id)
        self._list_ok = on_ok
        self._list_error = on_error

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.store.remove_job(job_id)


def in_thread(fn: Callable[[], Any], timeout: float = 15.0) -> Any:
    """Run ``fn`` off the main thread while the main thread pumps Qt events.

    The API marshals every handler onto the GUI thread and the caller waits, so
    a test that simply called the client here would deadlock: the thread the
    handler needs would be the one blocked on the reply.
    """
    from PyQt6.QtWidgets import QApplication

    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.time() + timeout
    while worker.is_alive() and time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    worker.join(1.0)
    if worker.is_alive():
        raise AssertionError("the request never finished")
    if "error" in box:
        raise box["error"]
    return box.get("value")
