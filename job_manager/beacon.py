"""This process's entry in the instance registry, and its answers to requests.

See :mod:`job_manager.instances`. The plugin, the standalone monitor and the
tray process each run one, with their own idea of what "show the monitor"
means.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, QTimer

from . import instances


class InstanceBeacon(QObject):
    """Registers this process once and runs what other launches ask."""

    def __init__(
        self, data_dir: str, role: str, handlers: Optional[Dict[str, Callable[[], None]]] = None
    ) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.role = role
        self.handlers = dict(handlers or {})
        self._started = 0.0
        self._server: Optional[instances.LivenessServer] = None
        self._request_timer = QTimer(self)
        self._request_timer.setInterval(int(instances.REQUEST_POLL_SECONDS * 1000))
        self._request_timer.timeout.connect(self.check_requests)

    def start(self) -> None:
        import time

        if self._server is not None:
            return
        self._started = time.time()
        server = None
        try:
            server = instances.LivenessServer()
            instances.register_instance(self.data_dir, self.role, self._started, server)
        except OSError:
            if server is not None:
                server.close()
            logging.warning("Job Manager: instance registration failed", exc_info=True)
            return
        self._server = server
        self._request_timer.start()

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
        self._request_timer.stop()
        instances.unregister_instance(self.data_dir)
        if self._server is not None:
            self._server.close()
            self._server = None


__all__ = ["InstanceBeacon"]
