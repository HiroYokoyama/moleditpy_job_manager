"""This process's entry in the instance registry, and its answers to requests.

See :mod:`job_manager.instances`. The plugin, the standalone monitor and the
tray process each run one, with their own idea of what "show the monitor"
means.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QObject, QTimer

from . import instances


class InstanceBeacon(QObject):
    """Keeps this process's heartbeat fresh and runs what other launches ask."""

    def __init__(
        self, data_dir: str, role: str, handlers: Optional[Dict[str, Callable[[], None]]] = None
    ) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.role = role
        self.handlers = dict(handlers or {})
        self._started = 0.0
        self._beat_timer = QTimer(self)
        self._beat_timer.setInterval(int(instances.HEARTBEAT_SECONDS * 1000))
        self._beat_timer.timeout.connect(self.beat)
        # Separately and faster: a launch waiting on us is a person waiting for
        # a window, and a two-second heartbeat would be felt.
        self._request_timer = QTimer(self)
        self._request_timer.setInterval(int(instances.REQUEST_POLL_SECONDS * 1000))
        self._request_timer.timeout.connect(self.check_requests)

    def start(self) -> None:
        import time

        self._started = time.time()
        self.beat()
        self._beat_timer.start()
        self._request_timer.start()

    def beat(self) -> None:
        try:
            instances.write_heartbeat(self.data_dir, self.role, self._started or None)
        except OSError:
            logging.debug("Job Manager: heartbeat not written", exc_info=True)

    def check_requests(self) -> None:
        action = instances.take_request(self.data_dir)
        if not action:
            return
        handler = self.handlers.get(action)
        if handler is None:
            logging.debug("Job Manager: ignored request %r", action)
            return
        try:
            handler()
        except Exception:
            logging.exception("Job Manager: request %r failed", action)

    def stop(self) -> None:
        self._beat_timer.stop()
        self._request_timer.stop()
        instances.remove_heartbeat(self.data_dir)


__all__ = ["InstanceBeacon"]
