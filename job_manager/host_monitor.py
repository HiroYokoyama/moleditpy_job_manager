"""Live load and memory for every host, while the window is open.

Deliberately not part of polling. A load average is worth a command every few
seconds when somebody is watching it and worth nothing when nobody is, so the
timer runs only while this window is open and stops the moment it closes --
a Job Manager left open overnight touches no login node on its account.

The transport is held open per host for the same reason it is not: at a
two-second cadence, building and tearing down a connection each time would
cost more than the measurement. paramiko then reuses one real SSH session;
OpenSSH still spawns its own process per command, which is why the interval is
adjustable and why nothing here runs unless the window is up.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION, host_stats
from .credentials import needs_password
from .models import SCHEDULER_WINDOWS, HostProfile
from .window_utils import make_independent
from .tasks import run_async

#: How many samples a graph keeps. At the default interval that is about two
#: minutes of history, which is enough to see a job start.
HISTORY = 60

DEFAULT_INTERVAL_SECONDS = 2


class Sparkline(QWidget):
    """A small filled line chart of values in 0..1, oldest on the left."""

    def __init__(self, color: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.color = color
        self.values: Deque[float] = deque(maxlen=HISTORY)
        self.setMinimumHeight(34)

    def sizeHint(self) -> QSize:
        return QSize(220, 34)

    def add(self, value: float) -> None:
        self.values.append(max(0.0, min(1.0, float(value))))
        self.update()

    def clear(self) -> None:
        self.values.clear()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 1, -1, -1)

        # A faint bed, so an empty graph still reads as a graph rather than as
        # a piece of the dialog that failed to draw.
        bed = QColor(self.palette().mid().color())
        bed.setAlpha(40)
        painter.fillRect(rect, bed)

        if len(self.values) < 2:
            painter.setPen(QPen(self.palette().mid().color()))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "...")
            return

        step = rect.width() / (len(self.values) - 1)
        points = [
            (rect.left() + index * step, rect.bottom() - value * rect.height())
            for index, value in enumerate(self.values)
        ]

        fill = QPainterPath()
        fill.moveTo(points[0][0], rect.bottom())
        for x, y in points:
            fill.lineTo(x, y)
        fill.lineTo(points[-1][0], rect.bottom())
        fill.closeSubpath()
        shade = QColor(self.color)
        shade.setAlpha(60)
        painter.fillPath(fill, shade)

        line = QPainterPath()
        line.moveTo(*points[0])
        for x, y in points[1:]:
            line.lineTo(x, y)
        painter.setPen(QPen(self.color, 1.6))
        painter.drawPath(line)


class HostCard(QFrame):
    """One host: its name, what it is doing, and two graphs."""

    def __init__(self, host: HostProfile, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.host = host
        self.setFrameShape(QFrame.Shape.StyledPanel)

        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(10)

        self.lbl_name = QLabel(f"<b>{host.name}</b>")
        self.lbl_target = QLabel(host.target)
        self.lbl_target.setStyleSheet("color: palette(mid);")
        self.lbl_summary = QLabel("waiting...")
        self.lbl_summary.setWordWrap(True)

        # Load in the theme's own highlight, memory in a second colour that
        # keeps its contrast in a dark palette as well as a light one.
        self.graph_load = Sparkline(self.palette().highlight().color())
        self.graph_memory = Sparkline(QColor("#c77d1a"))
        self.lbl_load = QLabel("load")
        self.lbl_memory = QLabel("memory")
        for label in (self.lbl_load, self.lbl_memory):
            label.setStyleSheet("color: palette(mid);")

        grid.addWidget(self.lbl_name, 0, 0)
        grid.addWidget(self.lbl_target, 0, 1, 1, 2)
        grid.addWidget(self.lbl_summary, 1, 0, 1, 3)
        grid.addWidget(self.lbl_load, 2, 0)
        grid.addWidget(self.graph_load, 2, 1, 1, 2)
        grid.addWidget(self.lbl_memory, 3, 0)
        grid.addWidget(self.graph_memory, 3, 1, 1, 2)
        grid.setColumnStretch(2, 1)

    def show_stats(self, stats: host_stats.HostStats) -> None:
        self.lbl_summary.setText(stats.summary)
        self.setToolTip(stats.summary)
        if not stats.ok:
            return
        self.graph_load.add(stats.load_fraction)
        self.graph_memory.add(stats.memory_fraction)
        self.lbl_load.setText(f"load {stats.load[0]:.2f}" if stats.load else "load")
        if stats.mem_total_mb and stats.mem_free_mb:
            self.lbl_memory.setText(f"memory {stats.memory_fraction * 100:.0f}%")

    def show_error(self, message: str) -> None:
        self.lbl_summary.setText(message.splitlines()[0] if message else "no answer")


class HostMonitorDialog(QDialog):
    """A card per host, refreshed on a timer while this window is open."""

    def __init__(self, service, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Hosts at work")
        make_independent(self)
        self.resize(560, 620)
        self.cards: Dict[str, HostCard] = {}
        #: Held open while this window is: see the module docstring.
        self._transports: Dict[str, object] = {}
        #: Hosts with a probe still in flight, so a slow host does not queue up
        #: one worker per tick until the pool is full of them.
        self._busy: set = set()
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sample_all)
        self._timer.start(DEFAULT_INTERVAL_SECONDS * 1000)
        self._sample_all()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Refresh every"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(1, 60)
        self.spin_interval.setSuffix(" s")
        self.spin_interval.setValue(DEFAULT_INTERVAL_SECONDS)
        self.spin_interval.setToolTip(
            "How often each host is asked for its load and memory.\n\n"
            "One command per host per tick, and only while this window is "
            "open. On a shared login node, slower is politer."
        )
        self.spin_interval.valueChanged.connect(
            lambda seconds: self._timer.setInterval(int(seconds) * 1000)
        )
        top.addWidget(self.spin_interval)
        top.addStretch(1)
        layout.addLayout(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        self.column = QVBoxLayout(body)
        self.column.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        for host in self.service.store.host_list():
            card = HostCard(host)
            self.cards[host.id] = card
            self.column.addWidget(card)
        if not self.cards:
            self.column.addWidget(QLabel("No hosts yet. Add one under Hosts..."))
        self.column.addStretch(1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    # --- sampling -----------------------------------------------------------

    def _hosts(self) -> List[HostProfile]:
        return [
            host
            for host in self.service.store.host_list()
            if host.id in self.cards and not needs_password(self.service, host)
        ]

    def _transport_for(self, host: HostProfile):
        transport = self._transports.get(host.id)
        if transport is None:
            transport = self.service.transport_for(host)
            self._transports[host.id] = transport
        return transport

    def _sample_all(self) -> None:
        for host in self._hosts():
            if host.id in self._busy:
                # Still waiting on the last one. Skipping a tick is the right
                # answer for a host slower than the interval; stacking probes
                # would fill the pool and make it slower still.
                continue
            self._sample(host)

    def _sample(self, host: HostProfile) -> None:
        card = self.cards.get(host.id)
        if card is None:
            return
        self._busy.add(host.id)
        command = host_stats.command_for(host.scheduler == SCHEDULER_WINDOWS)
        host_id = host.id

        def work() -> str:
            transport = self._transport_for(host)
            result = transport.run(command, timeout=max(5, int(host.connect_timeout or 10)))
            return result.stdout

        def ok(text: str) -> None:
            self._busy.discard(host_id)
            if host_id in self.cards:
                self.cards[host_id].show_stats(host_stats.parse(text))

        def failed(message: str) -> None:
            self._busy.discard(host_id)
            # A failed probe drops the connection: the next tick builds a new
            # one rather than reusing a socket the far end has already closed.
            self._close_transport(host_id)
            if host_id in self.cards:
                self.cards[host_id].show_error(message)

        run_async(self.service.pool, work, on_success=ok, on_error=failed)

    # --- teardown -----------------------------------------------------------

    def _close_transport(self, host_id: str) -> None:
        transport = self._transports.pop(host_id, None)
        if transport is None:
            return
        try:
            transport.close()
        except Exception:  # pragma: no cover - closing must never raise here
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        """Stop the timer and hand every connection back."""
        self._timer.stop()
        for host_id in list(self._transports):
            self._close_transport(host_id)
        super().closeEvent(event)

    def reject(self) -> None:
        # Esc closes a dialog without a closeEvent, which would leave the timer
        # running and every connection open for the life of the session.
        self._timer.stop()
        for host_id in list(self._transports):
            self._close_transport(host_id)
        super().reject()


__all__ = ["HostCard", "HostMonitorDialog", "Sparkline"]
