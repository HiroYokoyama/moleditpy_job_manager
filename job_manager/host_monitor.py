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

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION, host_stats
from .credentials import needs_password
from .models import SCHEDULER_WINDOWS, HostProfile
from .theme import (
    CY_ACCENT,
    CY_ACCENT2,
    CY_AMBER,
    CY_GREEN,
    CY_GREY,
)

from .window_utils import make_independent
from .tasks import run_async

#: How many samples a graph keeps. At the default interval that is about two
#: minutes of history, which is enough to see a job start.
HISTORY = 60

#: Two seconds is right for a backend that keeps its connection: paramiko
#: reuses one SSH session, and the local backend runs a subprocess. It is far
#: too fast for OpenSSH, which spawns a whole ssh process per command -- on
#: Windows it cannot multiplex at all -- so every tick is a fresh TCP connect,
#: handshake and authentication to the same machine. That costs more than the
#: measurement, and a burst of them trips sshd's own connection throttling,
#: which arrives here as a timeout on a host that is perfectly healthy.
DEFAULT_INTERVAL_SECONDS = 2
OPENSSH_INTERVAL_SECONDS = 10

#: A host that fails is asked less often, doubling up to this, rather than
#: every tick for as long as the window is open.
MAX_BACKOFF_TICKS = 16

#: Dark theme palette and styling definitions for the Host Monitor window.
_DARK = {
    "window": "#16181a",
    "base": "#1f2327",
    "alternate": "#282c34",
    "text": "#f0f6fc",
    "mid": "#8b949e",
    "highlight": "#2979ff",
}


def dark_palette(base: Optional[QPalette] = None) -> QPalette:
    """A dark palette built from the current palette."""
    palette = QPalette(base) if base is not None else QPalette()
    window = QColor(_DARK["window"])
    base = QColor(_DARK["base"])
    text = QColor(_DARK["text"])
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button):
        palette.setColor(role, window)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(_DARK["alternate"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, base)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.ToolTipText,
    ):
        palette.setColor(role, text)
    palette.setColor(QPalette.ColorRole.Mid, QColor(_DARK["mid"]))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(_DARK["alternate"]))
    palette.setColor(QPalette.ColorRole.Light, QColor(_DARK["alternate"]))
    palette.setColor(QPalette.ColorRole.Dark, window.darker(140))
    palette.setColor(QPalette.ColorRole.Shadow, window.darker(200))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(_DARK["mid"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(_DARK["highlight"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, window)
    return palette


_DARK_DIALOG_STYLE = f"""
HostMonitorDialog, QDialog {{
    background-color: {_DARK["window"]};
    color: {_DARK["text"]};
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background-color: {_DARK["window"]};
    background: {_DARK["window"]};
}}
QLabel {{
    color: {_DARK["text"]};
}}
QPushButton {{
    background-color: #21262d;
    color: {_DARK["text"]};
    border: 1px solid #363b42;
    border-radius: 5px;
    padding: 5px 14px;
    min-height: 20px;
}}
QPushButton:hover {{
    background-color: #30363d;
    border-color: {CY_ACCENT};
}}
QPushButton:checked {{
    background-color: #1e3a5f;
    border-color: {CY_ACCENT};
    color: {CY_ACCENT};
}}
QPushButton:disabled {{
    color: {_DARK["mid"]};
    background-color: #17191c;
    border-color: #2a2e33;
}}
QSpinBox {{
    background-color: #0d1117;
    color: {_DARK["text"]};
    border: 1px solid #363b42;
    border-radius: 4px;
    padding: 2px 20px 2px 4px;
    min-height: 22px;
}}
QSpinBox:disabled {{
    color: {_DARK["mid"]};
    background-color: #17191c;
}}
QSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
}}
QSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
}}
QLineEdit, QComboBox {{
    background-color: #0d1117;
    color: {_DARK["text"]};
    border: 1px solid #363b42;
    border-radius: 4px;
    padding: 3px 6px;
}}
QLineEdit:disabled, QComboBox:disabled {{
    color: {_DARK["mid"]};
    background-color: #17191c;
}}
"""

_LIGHT_DIALOG_STYLE = f"""
HostMonitorDialog, QDialog {{
    background-color: #f6f8fa;
    color: #1f2328;
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background-color: #f6f8fa;
    background: #f6f8fa;
}}
QLabel {{
    color: #1f2328;
}}
QPushButton {{
    background-color: #f6f8fa;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 5px;
    padding: 5px 14px;
    min-height: 20px;
}}
QPushButton:hover {{
    background-color: #eaeef2;
    border-color: {CY_ACCENT};
}}
QPushButton:checked {{
    background-color: #ddf4ff;
    border-color: {CY_ACCENT};
    color: {CY_ACCENT};
}}
QPushButton:disabled {{
    color: #8b949e;
    background-color: #eceff2;
    border-color: #d0d7de;
}}
QSpinBox {{
    background-color: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 4px;
    padding: 2px 20px 2px 4px;
    min-height: 22px;
}}
QSpinBox:disabled {{
    color: #8b949e;
    background-color: #eceff2;
}}
QSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
}}
QSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
}}
QLineEdit, QComboBox {{
    background-color: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 4px;
    padding: 3px 6px;
}}
QLineEdit:disabled, QComboBox:disabled {{
    color: #8b949e;
    background-color: #eceff2;
}}
"""


#: One colour per thing measured, and the bar and the graph under it share it:
#: load green, memory blue. Colouring the bar by how full it was instead made
#: the pair look like two unrelated readings, and a bar that changes hue as the
#: value moves is harder to compare across cards than one that does not.
GRAPH_CPU = QColor(CY_GREEN)
GRAPH_LOAD = GRAPH_CPU
GRAPH_MEMORY = QColor(CY_ACCENT2)


class Meter(QWidget):
    """A vertical bar displaying resource usage."""

    def __init__(self, caption: str, color: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.caption = caption
        self.color = color
        self.fraction = 0.0
        self.detail = "-"
        self._dark = False
        self.setMinimumHeight(132)
        self.setMinimumWidth(67)
        self.setMaximumWidth(132)

    def sizeHint(self) -> QSize:
        return QSize(100, 156)

    def set_dark(self, dark: bool = False) -> None:
        self._dark = dark
        self.update()

    def show_value(self, fraction: float, detail: str, caption: str = "", tip: str = "") -> None:
        self.fraction = max(0.0, min(1.0, float(fraction)))
        self.detail = detail
        if caption:
            self.caption = caption
        self.setToolTip(tip or detail)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        metrics = painter.fontMetrics()
        line = metrics.height()
        column = QRectF(self.rect().adjusted(6, 2, -6, -(2 * line + 6)))
        radius = 4.0

        track = QColor(255, 255, 255, 25) if self._dark else QColor(0, 0, 0, 25)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(column, radius, radius)

        if self.fraction > 0:
            filled = QRectF(column)
            height = max(2 * radius, column.height() * self.fraction)
            filled.setTop(column.bottom() - height)

            glow = QColor(self.color)
            glow.setAlpha(60)
            painter.setBrush(glow)
            painter.drawRoundedRect(filled.adjusted(-3, -3, 3, 3), radius + 3, radius + 3)

            painter.setBrush(self.color)
            painter.drawRoundedRect(filled, radius, radius)

            cap = QColor(self.color).lighter(150)
            painter.setBrush(cap)
            top = QRectF(filled)
            top.setHeight(min(3.0, filled.height()))
            painter.drawRoundedRect(top, 1.5, 1.5)

        text_color = QColor("#f0f6fc" if self._dark else "#1f2328")
        subtext_color = QColor("#8b949e" if self._dark else "#656d76")

        number = QFont(painter.font())
        number.setBold(True)
        number.setPointSizeF(max(9.0, number.pointSizeF() * 1.15))
        painter.setFont(number)
        painter.setPen(QPen(text_color))
        painter.drawText(
            QRectF(self.rect()).adjusted(0, column.height() + 2, 0, -line),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self.detail,
        )
        painter.setFont(QFont(self.font()))
        painter.setPen(QPen(subtext_color))
        painter.drawText(
            QRectF(self.rect()).adjusted(0, column.height() + 2 + line, 0, 0),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self.caption,
        )


class Sparkline(QWidget):
    """History graph displaying samples over time."""

    def __init__(self, color: QColor, caption: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.color = color
        self.caption = caption
        self.values: Deque[float] = deque(maxlen=HISTORY)
        self._dark = False
        self.setMinimumHeight(70)

    def sizeHint(self) -> QSize:
        return QSize(180, 84)

    def set_dark(self, dark: bool = False) -> None:
        self._dark = dark
        self.update()

    def add(self, value: float) -> None:
        self.values.append(max(0.0, min(1.0, float(value))))
        self.update()

    def clear(self) -> None:
        self.values.clear()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 1, -1, -1)

        guide_color = QColor("#8b949e" if self._dark else "#656d76")
        QColor("#f0f6fc" if self._dark else "#1f2328")

        guide = QColor(guide_color)
        guide.setAlpha(60)
        painter.setPen(QPen(guide, 1.0, Qt.PenStyle.DotLine))
        for share in (0.25, 0.5, 0.75):
            y = int(rect.bottom() - share * rect.height())
            painter.drawLine(rect.left() + 2, y, rect.right() - 2, y)

        if self.caption:
            painter.setPen(QPen(guide_color))
            painter.drawText(
                rect.adjusted(6, 2, -6, 0),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                self.caption,
            )

        if len(self.values) < 2:
            painter.setPen(QPen(guide_color))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "collecting...")
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
        halo = QColor(self.color)
        halo.setAlpha(70)
        painter.setPen(QPen(halo, 4.0))
        painter.drawPath(line)
        painter.setPen(QPen(self.color, 1.6))
        painter.drawPath(line)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color).lighter(130))
        painter.drawEllipse(QRectF(points[-1][0] - 2.5, points[-1][1] - 2.5, 5, 5))


class HostCard(QFrame):
    """One host: what it is doing now, and its history on a double click."""

    def __init__(self, host: HostProfile, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.host = host
        self.setObjectName("hostCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._surface = QColor()
        self._edge = QColor()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        header = QHBoxLayout()
        self.lbl_name = QLabel(f"<b>{host.name}</b>")
        self.lbl_state = QLabel("waiting...")
        header.addWidget(self.lbl_name)
        header.addStretch(1)
        header.addWidget(self.lbl_state)
        outer.addLayout(header)

        self.lbl_target = QLabel(host.target)
        self.lbl_target.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_target.setMinimumWidth(0)
        self.lbl_target.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(self.lbl_target)

        #: 1-minute load average, shown small and grey beside the target --
        #: a second, coarser reading next to the instantaneous CPU meter below.
        self.lbl_load_avg = QLabel("")
        font = self.lbl_load_avg.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() * 0.85))
        self.lbl_load_avg.setFont(font)
        self.lbl_load_avg.setText("&nbsp;")
        outer.addWidget(self.lbl_load_avg)

        self.meter_cpu = Meter("CPU", GRAPH_CPU)
        self.meter_load = self.meter_cpu
        self.meter_memory = Meter("memory", GRAPH_MEMORY)
        columns = QHBoxLayout()
        columns.setSpacing(14)
        columns.addStretch(1)
        columns.addWidget(self.meter_cpu)
        columns.addWidget(self.meter_memory)
        columns.addStretch(1)
        outer.addLayout(columns)

        self.graph_cpu = Sparkline(GRAPH_CPU, "CPU")
        self.graph_load = self.graph_cpu
        self.graph_memory = Sparkline(GRAPH_MEMORY, "memory")
        for widget in (self.graph_cpu, self.graph_memory):
            widget.setVisible(False)
            outer.addWidget(widget)

        # Jobs running on this host -- locked to fixed 2-line height to prevent any layout jump.
        self.lbl_jobs = QLabel("&nbsp;")
        self.lbl_jobs.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_jobs.setWordWrap(True)
        self.lbl_jobs.setFixedHeight(36)
        self.lbl_jobs.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self.lbl_jobs)

        self.restyle()

    def restyle(self, palette: Optional[QPalette] = None, dark: Optional[bool] = None) -> None:
        """Recalculate the card's surface and border colours for the palette."""
        palette = palette or self.palette()
        if dark is None:
            dark = palette.window().color().lightness() < 128
        self._dark = dark
        bg = "#1f2327" if dark else "#ffffff"
        border = "#33383f" if dark else "#d0d7de"
        self._surface = QColor(bg)
        self._edge = QColor(border)
        if dark:
            self.lbl_name.setStyleSheet("color: #f0f6fc; font-weight: bold;")
            self.lbl_state.setStyleSheet("color: #8b949e;")
            self.lbl_target.setStyleSheet("color: #8b949e;")
            self.lbl_load_avg.setStyleSheet("color: #6e7681;")
        else:
            self.lbl_name.setStyleSheet("color: #1f2328; font-weight: bold;")
            self.lbl_state.setStyleSheet("color: #656d76;")
            self.lbl_target.setStyleSheet("color: #656d76;")
            self.lbl_load_avg.setStyleSheet("color: #8b949e;")
        self.setStyleSheet(
            f"QFrame#hostCard {{ background-color: {bg}; border: 1px solid {border}; border-radius: 10px; }}"
        )
        self.meter_cpu.set_dark(dark)
        self.meter_memory.set_dark(dark)
        self.graph_cpu.set_dark(dark)
        self.graph_memory.set_dark(dark)
        self.setPalette(palette)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        """Draw the rounded card surface and border."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._edge, 1.0))
        painter.setBrush(self._surface)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10.0, 10.0)

    # --- expanding ----------------------------------------------------------

    @property
    def expanded(self) -> bool:
        return not self.graph_cpu.isHidden()

    def set_expanded(self, expanded: bool) -> None:
        self.graph_cpu.setVisible(expanded)
        self.graph_memory.setVisible(expanded)
        self.meter_cpu.setVisible(not expanded)
        self.meter_memory.setVisible(not expanded)

    def show_jobs(self, jobs: list) -> None:
        """Update the running job display: shows running job on line 1, and task progress on line 2."""
        import html as _html

        if not jobs:
            self.lbl_jobs.setText("&nbsp;")
            return

        total = len(jobs)
        done = sum(1 for j in jobs if getattr(j, "state", "").upper() in ("DONE", "COMPLETED"))
        running = [
            j
            for j in jobs
            if getattr(j, "is_active", False) and getattr(j, "state", "").upper() == "RUNNING"
        ]
        queued = sum(
            1
            for j in jobs
            if getattr(j, "is_active", False)
            and getattr(j, "state", "").upper() in ("QUEUED", "PENDING", "SUBMITTED")
        )

        if not running and queued == 0 and done == 0:
            self.lbl_jobs.setText("&nbsp;")
            return

        line1 = "&nbsp;"
        if running:
            name = running[0].name or "Job"
            if len(name) > 38:
                name = name[:35] + "..."
            name_html = _html.escape(name)
            line1 = f"<span style='color:{CY_GREEN};font-weight:bold'>▶ {name_html}</span>"
        elif queued > 0:
            line1 = f"<span style='color:#8b949e'>⏳ {queued} queued</span>"
        elif done == total and total > 0:
            line1 = f"<span style='color:#8b949e'>✔ {total}/{total} completed</span>"

        line2 = f"<span style='color:#8b949e;font-size:11px'>task {done}/{total} done</span>"

        self.lbl_jobs.setText(f"<div style='line-height:1.2'>{line1}<br>{line2}</div>")

    # --- what a sample changes ----------------------------------------------

    def show_stats(self, stats: host_stats.HostStats) -> None:
        self.setToolTip(stats.summary)
        if not stats.ok:
            self.show_error(stats.summary)
            return
        # Thread usability: load_fraction is computed against the thread
        # (logical CPU) count now, not the physical core count underneath it.
        cores = f"{stats.cores} cores" if stats.cores else ""
        if stats.threads and stats.cores and stats.threads > stats.cores > 0:
            cores += f", {stats.threads} threads"
        elif stats.threads and not stats.cores:
            cores = f"{stats.threads} threads"
        self.lbl_state.setText(cores)

        load = stats.load[0] if stats.load else 0.0
        threads = stats.threads or stats.cores
        if threads:
            self.meter_cpu.show_value(
                stats.load_fraction,
                f"{stats.load_fraction * 100:.0f}%",
                f"of {threads} threads",
                f"{load:.2f} of {threads} threads",
            )
        else:
            self.meter_cpu.show_value(
                0.0, f"{load:.2f}", "CPU", "the host did not report its threads"
            )

        if stats.load:
            self.lbl_load_avg.setText(f"load avg {stats.load[0]:.2f}")
            self.lbl_load_avg.setToolTip(
                "1-minute load average, as the host reports it -- separate "
                "from the CPU meter above, which is instantaneous usage."
            )
        else:
            self.lbl_load_avg.setText("&nbsp;")
            self.lbl_load_avg.setToolTip("")

        total = f"{stats.mem_total_mb / 1024:.1f} GB" if stats.mem_total_mb else ""
        if stats.mem_total_mb and stats.mem_free_mb:
            self.meter_memory.show_value(
                stats.memory_fraction,
                f"{stats.memory_fraction * 100:.0f}%",
                f"of {total}",
                f"{stats.mem_used_mb / 1024:.1f} of {total} in use",
            )
        elif stats.mem_total_mb:
            self.meter_memory.show_value(
                0.0, "-", f"of {total}", f"{total} total, usage not reported"
            )
        else:
            self.meter_memory.show_value(0.0, "-", "memory", "the host did not report its memory")

        self.graph_cpu.add(stats.load_fraction)
        self.graph_memory.add(stats.memory_fraction)

    def show_error(self, message: str) -> None:
        first = message.splitlines()[0] if message else "no answer"
        self.lbl_state.setText(first)
        self.setToolTip(first)
        self.meter_cpu.show_value(0.0, "-")
        self.meter_memory.show_value(0.0, "-")
        self.lbl_load_avg.setText("&nbsp;")


class HostMonitorDialog(QDialog):
    """A card per host, refreshed on a timer while this window is open."""

    #: One card fits comfortably in this much width. Fewer, wider columns are
    #: better than many cramped ones: a bar too short to read is worse than a
    #: second row.
    CARD_WIDTH = 320

    def __init__(self, service, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Host Monitor")
        make_independent(self)
        # Wide enough for two columns of cards from the start: one column looks
        # like a list of three things, and two is where the layout reads as a
        # panel you can compare machines across.
        self.resize(2 * self.CARD_WIDTH + 60, 660)
        self.cards: Dict[str, HostCard] = {}
        #: The palette this window was born with, so the dark toggle has
        #: something exact to go back to.
        self._light_palette = QPalette(self.palette())
        self._scroll: Optional[QScrollArea] = None
        #: Held open while this window is: see the module docstring.
        self._transports: Dict[str, object] = {}
        #: Hosts with a probe still in flight, so a slow host does not queue up
        #: one worker per tick until the pool is full of them.
        self._busy: set = set()
        #: Ticks still to skip for a host that failed, and the size of the
        #: skip it earned. Both cleared by a sample that works.
        self._skip_ticks: Dict[str, int] = {}
        self._backoff: Dict[str, int] = {}
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sample_all)
        # The stored choice wins: the per-backend default is a starting point,
        # not a correction to apply over the top of somebody's setting every
        # time they open the window.
        stored = int(self.service.store.get_pref("host_monitor_interval", 0) or 0)
        # Blocked: setValue emits valueChanged, and letting that through here
        # would record the backend's default as the user's own choice the
        # moment the window opened -- so "not chosen yet" could never happen
        # twice.
        self.spin_interval.blockSignals(True)
        self.spin_interval.setValue(stored or self._default_interval())
        self.spin_interval.blockSignals(False)
        self._timer.start(self.spin_interval.value() * 1000)
        if self.btn_history.isChecked():
            self._set_history(True)
        # `setChecked` above happens before the signal connection, so the
        # stored choice does not emit `toggled` during construction. Apply
        # the complete window style explicitly for both modes; otherwise a
        # freshly launched monitor has native/light chrome until the user
        # toggles Dark once.
        self._set_dark(bool(self.btn_dark.isChecked()))
        self._sample_all()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Refresh every"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(1, 60)
        self.spin_interval.setSuffix(" s")
        self.spin_interval.setValue(DEFAULT_INTERVAL_SECONDS)
        self.spin_interval.setMaximum(300)
        self.spin_interval.setToolTip(
            "How often each host is asked for its load and memory.\n\n"
            "One command per host per tick, and only while this window is "
            "open. On a shared login node, slower is politer.\n\n"
            "The default follows the backend: two seconds where the connection "
            "is kept (paramiko, this machine), ten for OpenSSH, which starts a "
            "whole ssh process per command and is rate-limited by the far end "
            "if asked faster. Whatever you set here is remembered."
        )
        self.spin_interval.valueChanged.connect(self._set_interval)
        top.addWidget(self.spin_interval)
        top.addStretch(1)
        self.btn_history = QPushButton("History")
        self.btn_history.setCheckable(True)
        self.btn_history.setToolTip(
            "Show the last two minutes under every card: load in green, memory "
            "in blue.\n\n"
            "The bars answer 'is there room on that machine?'. The graphs "
            "answer 'has it been like that long?', which is worth the space "
            "only while it is being asked."
        )
        self.btn_history.setChecked(
            bool(self.service.store.get_pref("host_monitor_history", False))
        )
        self.btn_history.toggled.connect(self._set_history)
        top.addWidget(self.btn_history)

        self.btn_dark = QPushButton("Dark")
        self.btn_dark.setCheckable(True)
        self.btn_dark.setToolTip(
            "Dark colours for this window only.\n\n"
            "For the case this window is built for: left up on a second screen "
            "beside something else. MoleditPy's own theme is not touched."
        )
        self.btn_dark.setChecked(bool(self.service.store.get_pref("host_monitor_dark", False)))
        self.btn_dark.toggled.connect(self._set_dark)
        top.addWidget(self.btn_dark)
        layout.addLayout(top)

        scroll = QScrollArea()
        self._scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        scroll.setWidget(self.body)
        layout.addWidget(scroll, 1)

        for host in self.service.store.host_list():
            card = HostCard(host)
            if not getattr(host, "enabled", True):
                card.setEnabled(False)
                card.lbl_state.setText("disabled")
                # HostCard paints its surface and labels with fixed colours,
                # not through the palette, so setEnabled() alone leaves it
                # looking identical to an enabled card. An opacity effect dims
                # it regardless of how its colours are drawn.
                effect = QGraphicsOpacityEffect(card)
                effect.setOpacity(0.45)
                card.setGraphicsEffect(effect)
            self.cards[host.id] = card
        if not self.cards:
            self.grid.addWidget(QLabel("No hosts yet. Add one under Hosts..."), 0, 0)
        self._relayout()
        self.service.jobs_changed.connect(self._refresh_card_jobs)
        self.service.job_updated.connect(self._on_job_updated)
        self._refresh_card_jobs()

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)

        self._jobs_bar = _ActiveJobsBar(self.service)
        layout.addWidget(self._jobs_bar)
        layout.addWidget(box)

    def _disconnect_signals(self) -> None:
        """Disconnect service signals to prevent memory leaks on re-open."""
        try:
            self.service.jobs_changed.disconnect(self._refresh_card_jobs)
        except Exception:
            pass
        try:
            self.service.job_updated.disconnect(self._on_job_updated)
        except Exception:
            pass
        if hasattr(self, "_jobs_bar") and self._jobs_bar is not None:
            self._jobs_bar.teardown()

    def _on_job_updated(self, _job_id: str = "") -> None:
        self._refresh_card_jobs()

    def _refresh_card_jobs(self) -> None:
        """Push each host's active jobs into its card's jobs strip."""
        jobs_by_host: dict = {}
        for job in self.service.store.jobs.values():
            if job.host_id:
                jobs_by_host.setdefault(job.host_id, []).append(job)
            if job.host_name:
                jobs_by_host.setdefault(job.host_name, []).append(job)
        for host_id, card in self.cards.items():
            card_jobs = jobs_by_host.get(card.host.id) or jobs_by_host.get(card.host.name, [])
            card.show_jobs(card_jobs)

    def _columns(self) -> int:
        return max(1, min(len(self.cards) or 1, self.width() // self.CARD_WIDTH or 1))

    def _relayout(self) -> None:
        """Place the cards in as many columns as the window has room for."""
        columns = self._columns()
        if columns == getattr(self, "_laid_out_for", 0):
            return
        self._laid_out_for = columns
        for card in self.cards.values():
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards.values()):
            self.grid.addWidget(card, index // columns, index % columns)
        for column in range(self.grid.columnCount()):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        rows = (len(self.cards) + columns - 1) // columns
        for row in range(rows + 1):
            # Everything after the last row of cards absorbs the spare height,
            # so a card keeps the size it asked for instead of being stretched
            # down the viewport.
            self.grid.setRowStretch(row, 0 if row < rows else 1)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        super().resizeEvent(event)
        self._relayout()

    def _set_interval(self, seconds: int) -> None:
        """Apply the cadence and save setting."""
        self.service.store.set_pref("host_monitor_interval", int(seconds))
        self._timer.setInterval(int(seconds) * 1000)

    def _set_history(self, shown: bool) -> None:
        """Open or close the graphs on every card at once and save setting."""
        self.service.store.set_pref("host_monitor_history", bool(shown))
        for card in self.cards.values():
            card.set_expanded(shown)

    def _set_dark(self, dark: bool) -> None:
        """Repaint this window in dark or light mode and save setting."""
        self.service.store.set_pref("host_monitor_dark", bool(dark))
        pal = dark_palette(self._light_palette) if dark else self._light_palette
        self.setPalette(pal)

        # Both branches set an explicit stylesheet -- an empty one for "light"
        # left buttons and fields on whatever native chrome the platform style
        # drew, which is a different size than the dark-mode style's own
        # padding, so toggling the button visibly changed size. _LIGHT_DIALOG_STYLE
        # exists for exactly this and was previously unused.
        self.setStyleSheet(_DARK_DIALOG_STYLE if dark else _LIGHT_DIALOG_STYLE)
        self.setAutoFillBackground(True)
        if self._scroll is not None:
            self._scroll.viewport().setAutoFillBackground(True)
            self._scroll.viewport().setPalette(pal)
            if hasattr(self, "body") and self.body is not None:
                self.body.setPalette(pal)
        # Qt caches each widget's resolved style properties; a bare
        # setStyleSheet() on the dialog does not always invalidate them on
        # children that already painted once, which is what made a toggle look
        # like it "stuck" on the previous mode until something else forced a
        # repaint. unpolish/polish forces every child to recompute.
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        for child in self.findChildren(QWidget):
            style.unpolish(child)
            style.polish(child)
            child.update()
        # polish() can synthesize palette roles from the stylesheet's own
        # background-color (Qt keeps a QSS-styled widget's QPalette in sync
        # with what it paints), which silently drifted the window's palette
        # away from ``pal`` -- most visibly, toggling dark off no longer gave
        # back the exact palette the window started with. Setting it again
        # after polish is what actually makes it win.
        self.setPalette(pal)
        if self._scroll is not None:
            self._scroll.viewport().setPalette(pal)
            if hasattr(self, "body") and self.body is not None:
                self.body.setPalette(pal)
        for card in self.cards.values():
            card.restyle(pal, dark=dark)
        if hasattr(self, "_jobs_bar") and self._jobs_bar is not None:
            self._jobs_bar.refresh()
        self.update()

    # --- sampling -----------------------------------------------------------

    def _default_interval(self) -> int:
        """The cadence the slowest backend in the list can stand."""
        from .models import BACKEND_OPENSSH

        hosts = list(self.service.store.host_list())
        if any(host.backend == BACKEND_OPENSSH for host in hosts):
            return OPENSSH_INTERVAL_SECONDS
        return DEFAULT_INTERVAL_SECONDS

    def _hosts(self) -> List[HostProfile]:
        return [
            host
            for host in self.service.store.host_list()
            if host.id in self.cards
            and getattr(host, "enabled", True)
            and not needs_password(self.service, host)
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
            waiting = self._skip_ticks.get(host.id, 0)
            if waiting:
                # Backing off after a failure. Asking an unreachable host every
                # tick for as long as the window is open is how a monitor turns
                # into a denial of service against its own cluster.
                self._skip_ticks[host.id] = waiting - 1
                continue
            self._sample(host)

    def _sample(self, host: HostProfile) -> None:
        card = self.cards.get(host.id)
        if card is None:
            return
        self._busy.add(host.id)
        command = host_stats.command_for(host.scheduler == SCHEDULER_WINDOWS)
        host_id = host.id
        # Resolve the transport on the GUI thread before handing off to a
        # worker. Reading/writing self._transports from multiple background
        # threads concurrently (one per host) without a lock is a data race;
        # resolving here means each worker gets a stable object to call into.
        try:
            transport = self._transport_for(host)
        except Exception as exc:
            self._busy.discard(host_id)
            if host_id in self.cards:
                self.cards[host_id].show_error(str(exc))
            return

        def work() -> str:
            result = transport.run(command, timeout=max(15, int(host.connect_timeout or 10)))
            return result.stdout

        def ok(text: str) -> None:
            self._busy.discard(host_id)
            self._backoff.pop(host_id, None)
            self._skip_ticks.pop(host_id, None)
            if host_id in self.cards:
                self.cards[host_id].show_stats(host_stats.parse(text))

        def failed(message: str) -> None:
            self._busy.discard(host_id)
            # A failed probe drops the connection: the next tick builds a new
            # one rather than reusing a socket the far end has already closed.
            self._close_transport(host_id)
            waited = min(MAX_BACKOFF_TICKS, max(1, self._backoff.get(host_id, 0) * 2 or 1))
            self._backoff[host_id] = waited
            self._skip_ticks[host_id] = waited
            if host_id in self.cards:
                seconds = waited * max(1, self.spin_interval.value())
                self.cards[host_id].show_error(f"{message} - retrying in {seconds}s")

        run_async(self.service.pool, work, on_success=ok, on_error=failed, quiet=True)

    # --- teardown -----------------------------------------------------------

    def _close_transport(self, host_id: str) -> None:
        """Hand a transport's teardown to the pool instead of closing it here.

        paramiko's close() sends a disconnect over the socket and can block on
        it -- normally milliseconds, but a host that has gone quiet (the exact
        case that just made this probe fail, or that the window is closing on
        with a probe still in flight) can leave it waiting on a stalled or
        already-dead connection. Popped from ``self._transports`` immediately
        either way, so a probe that is mid-flight for this host stops seeing
        it as open the moment this returns; only the network teardown itself
        moves off the GUI thread.
        """
        transport = self._transports.pop(host_id, None)
        if transport is None:
            return

        def close() -> None:
            try:
                transport.close()
            except Exception:  # pragma: no cover - closing must never raise here
                pass

        run_async(self.service.pool, close, quiet=True)

    def _save_settings(self) -> None:
        """Save user preferences for Host Monitor only upon closing."""
        try:
            self.service.store.set_pref("host_monitor_interval", int(self.spin_interval.value()))
            self.service.store.set_pref("host_monitor_history", bool(self.btn_history.isChecked()))
            self.service.store.set_pref("host_monitor_dark", bool(self.btn_dark.isChecked()))
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        """Stop the timer, save settings, and hand every connection back."""
        self._timer.stop()
        self._save_settings()
        self._disconnect_signals()
        for host_id in list(self._transports):
            self._close_transport(host_id)
        super().closeEvent(event)

    def reject(self) -> None:
        # Esc / Close button closes dialog without a closeEvent.
        self._timer.stop()
        self._save_settings()
        self._disconnect_signals()
        for host_id in list(self._transports):
            self._close_transport(host_id)
        super().reject()

    def accept(self) -> None:
        self._timer.stop()
        self._save_settings()
        self._disconnect_signals()
        for host_id in list(self._transports):
            self._close_transport(host_id)
        super().accept()

    def done(self, r: int) -> None:
        self._timer.stop()
        self._save_settings()
        self._disconnect_signals()
        for host_id in list(self._transports):
            self._close_transport(host_id)
        super().done(r)


class _ActiveJobsBar(QWidget):
    """Summary bar at the bottom of the Host Monitor showing overall counts.

    Keeps itself updated via the service's signals rather than the host-monitor
    timer: the job list changes on poll results, not on stats ticks.
    """

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 2)
        layout.setSpacing(10)

        self._lbl_count = QLabel()
        self._lbl_count.setStyleSheet(f"color: {CY_ACCENT}; font-weight: bold;")
        layout.addWidget(self._lbl_count)
        layout.addStretch(1)

        service.jobs_changed.connect(self.refresh)
        service.job_updated.connect(self._on_updated)
        self.refresh()

    def teardown(self) -> None:
        """Disconnect service signals; call when the parent dialog closes."""
        try:
            self.service.jobs_changed.disconnect(self.refresh)
        except Exception:
            pass
        try:
            self.service.job_updated.disconnect(self._on_updated)
        except Exception:
            pass

    def _on_updated(self, _job_id: str = "") -> None:
        self.refresh()

    def _display_state(self, job) -> str:
        """Mirrors JobTableModel.display_state without importing it."""
        store = self.service.store
        if store.chain_blocker(job) is not None:
            return "blocked"
        if (
            job.after_job_id
            and job.is_active
            and store.jobs.get(job.after_job_id) is not None
            and store.jobs[job.after_job_id].is_active
        ):
            return "queued"
        return job.state.lower()

    def refresh(self) -> None:
        active = list(self.service.store.active_jobs())
        total = len(active)
        if not total:
            self._lbl_count.setText(f"<span style='color:{CY_GREY};'>● no active jobs</span>")
            return

        running = sum(1 for j in active if self._display_state(j) == "running")
        remaining = total - running
        all_jobs = list(self.service.store.jobs.values())
        done_count = sum(1 for j in all_jobs if j.state in ("DONE", "FAILED"))
        total_all = len(all_jobs)

        parts = []
        if running:
            parts.append(
                f"<span style='color:{CY_GREEN};font-weight:bold'>▶ {running} running</span>"
            )
        if remaining:
            parts.append(f"<span style='color:{CY_AMBER};'>⧖ {remaining} remaining</span>")
        if total_all > 0:
            parts.append(
                f"<span style='color:{CY_GREY};'>(task {done_count}/{total_all} done)</span>"
            )
        self._lbl_count.setText("  ".join(parts))


__all__ = ["HostCard", "HostMonitorDialog", "Sparkline"]
