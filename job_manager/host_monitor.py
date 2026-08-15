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

#: A dark palette for this window only, for the case it is built for: a queue
#: left up on a second screen. It does not touch the application, which owns
#: its own theme, and the bar and graph colours were chosen to read on both.
_DARK = {
    "window": "#23272a",
    "base": "#2b2f33",
    "alternate": "#31363b",
    "text": "#e8eaed",
    "mid": "#8b9299",
    "highlight": "#3daee9",
}


def dark_palette(base: Optional[QPalette] = None) -> QPalette:
    """A dark palette built *from* the one in use, not from scratch.

    A default-constructed QPalette leaves every role this function does not
    name at its own defaults -- Light, Midlight, Dark, Shadow and the rest --
    and those, mixed with the few dark ones set here, are what made the window
    come back muddy brown when the toggle was turned off and on again.
    """
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


#: One colour per thing measured, and the bar and the graph under it share it:
#: load green, memory blue. Colouring the bar by how full it was instead made
#: the pair look like two unrelated readings, and a bar that changes hue as the
#: value moves is harder to compare across cards than one that does not.
GRAPH_LOAD = QColor("#66bb6a")
GRAPH_MEMORY = QColor("#64b5f6")


class Meter(QWidget):
    """A column that fills from the bottom: the default view of one number.

    Vertical because that is how a tank reads -- full is up -- and because two
    of them side by side can be compared at a glance without either one's
    length depending on how wide the card happens to be.
    """

    def __init__(self, caption: str, color: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.caption = caption
        self.color = color
        self.fraction = 0.0
        self.detail = "-"
        self.setMinimumHeight(110)
        self.setMinimumWidth(56)
        self.setMaximumWidth(110)

    def sizeHint(self) -> QSize:
        return QSize(84, 130)

    def show_value(
        self, fraction: float, detail: str, caption: str = "", tip: str = ""
    ) -> None:
        """``detail`` is printed under the column, ``caption`` under that.

        The caption carries what the bar is a fraction *of* -- "of 62.5 GB",
        "of 8 cores" -- because a percentage on its own does not say whether
        the machine that is 75% full has four gigabytes left or forty.
        """
        self.fraction = max(0.0, min(1.0, float(fraction)))
        self.detail = detail
        if caption:
            self.caption = caption
        self.setToolTip(tip or detail)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        metrics = painter.fontMetrics()
        line = metrics.height()
        # Two lines under the column: the percentage, then what it is of.
        column = QRectF(self.rect().adjusted(6, 2, -6, -(2 * line + 6)))
        radius = 4.0

        track = QColor(self.palette().mid().color())
        track.setAlpha(50)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(column, radius, radius)

        if self.fraction > 0:
            filled = QRectF(column)
            # Anchored to the bottom and grown upwards, never narrower than its
            # own corners.
            height = max(2 * radius, column.height() * self.fraction)
            filled.setTop(column.bottom() - height)

            # A glow behind it, then the column, then a lit top edge: three
            # cheap passes that make a flat rectangle read as something with a
            # light in it, without an image or a shader.
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

        number = QFont(painter.font())
        number.setBold(True)
        number.setPointSizeF(max(9.0, number.pointSizeF() * 1.15))
        painter.setFont(number)
        painter.setPen(QPen(self.palette().text().color()))
        painter.drawText(
            QRectF(self.rect()).adjusted(0, column.height() + 2, 0, -line),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self.detail,
        )
        painter.setFont(QFont(self.font()))
        painter.setPen(QPen(self.palette().mid().color()))
        painter.drawText(
            QRectF(self.rect()).adjusted(0, column.height() + 2 + line, 0, 0),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
            self.caption,
        )


class Sparkline(QWidget):
    """History left to right: oldest at the left, newest at the right.

    Time runs the way it is read, and the value is the height -- so a rising
    line is a machine filling up, which is the shape people already know from
    every other load graph they have seen.
    """

    def __init__(self, color: QColor, caption: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.color = color
        self.caption = caption
        self.values: Deque[float] = deque(maxlen=HISTORY)
        self.setMinimumHeight(70)

    def sizeHint(self) -> QSize:
        return QSize(180, 84)

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

        # A faint bed, rounded like the bars above it, so an empty graph still
        # reads as a graph rather than as a piece of the dialog that failed to
        # draw.
        bed = QColor(self.palette().mid().color())
        bed.setAlpha(40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bed)
        painter.drawRoundedRect(QRectF(rect), 4.0, 4.0)

        # Quarter lines, so a shape can be read as a level rather than only as
        # a shape. Faint enough to stay behind the line.
        guide = QColor(self.palette().mid().color())
        guide.setAlpha(60)
        painter.setPen(QPen(guide, 1.0, Qt.PenStyle.DotLine))
        for share in (0.25, 0.5, 0.75):
            y = int(rect.bottom() - share * rect.height())
            painter.drawLine(rect.left() + 2, y, rect.right() - 2, y)

        if self.caption:
            painter.setPen(QPen(QColor(self.palette().mid().color())))
            painter.drawText(
                rect.adjusted(6, 2, -6, 0),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                self.caption,
            )

        if len(self.values) < 2:
            painter.setPen(QPen(self.palette().mid().color()))
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
        # Twice: a wide, faint pass under a thin, solid one. That is what makes
        # a line look lit rather than merely coloured.
        halo = QColor(self.color)
        halo.setAlpha(70)
        painter.setPen(QPen(halo, 4.0))
        painter.drawPath(line)
        painter.setPen(QPen(self.color, 1.6))
        painter.drawPath(line)

        # And a dot on the newest sample: the eye should land on "now".
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color).lighter(130))
        painter.drawEllipse(QRectF(points[-1][0] - 2.5, points[-1][1] - 2.5, 5, 5))


class HostCard(QFrame):
    """One host: what it is doing now, and its history on a double click.

    The bars are the default because they answer the question people actually
    open this for -- is there room on that machine? -- at a glance and from
    across the room. The graphs answer a different one, "has it been like that
    long?", and are worth the space only when it is being asked.
    """

    def __init__(self, host: HostProfile, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.host = host
        self.setObjectName("hostCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.restyle()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        header = QHBoxLayout()
        self.lbl_name = QLabel(f"<b>{host.name}</b>")
        self.lbl_state = QLabel("waiting...")
        self.lbl_state.setStyleSheet("color: palette(mid);")
        header.addWidget(self.lbl_name)
        header.addStretch(1)
        header.addWidget(self.lbl_state)
        outer.addLayout(header)

        self.lbl_target = QLabel(host.target)
        self.lbl_target.setStyleSheet("color: palette(mid);")
        # Elided rather than wrapped: an address is one line, and a card that
        # grows a second one for a long user@host is a card of a different size
        # from its neighbours.
        self.lbl_target.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_target.setMinimumWidth(0)
        self.lbl_target.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(self.lbl_target)

        self.meter_load = Meter("load", GRAPH_LOAD)
        self.meter_memory = Meter("memory", GRAPH_MEMORY)
        columns = QHBoxLayout()
        columns.setSpacing(14)
        columns.addStretch(1)
        columns.addWidget(self.meter_load)
        columns.addWidget(self.meter_memory)
        columns.addStretch(1)
        outer.addLayout(columns)

        # Built now, shown on request: a card that has to be rebuilt to expand
        # would lose the history it is being asked to show.
        self.graph_load = Sparkline(GRAPH_LOAD, "load, last 2 min")
        self.graph_memory = Sparkline(GRAPH_MEMORY, "memory")
        for widget in (self.graph_load, self.graph_memory):
            widget.setVisible(False)
            outer.addWidget(widget)

    def restyle(self, palette: Optional[QPalette] = None) -> None:
        """Rounded, faintly lit, and readable on either palette.

        The palette is passed in rather than read from ``self``: a widget that
        carries a style sheet resolves its own palette through it, so asking
        the card what colours it has returns what the sheet already said and
        the card never followed the window into dark mode.
        """
        palette = palette or self.palette()
        base = palette.base().color()
        window = palette.window().color()
        dark = window.lightness() < 128
        surface = base.lighter(108) if dark else base
        edge = QColor(GRAPH_MEMORY)
        edge.setAlpha(90 if dark else 60)
        self.setStyleSheet(
            "QFrame#hostCard {"
            f" background: rgba({surface.red()},{surface.green()},{surface.blue()},"
            f"{235 if dark else 255});"
            f" border: 1px solid rgba({edge.red()},{edge.green()},{edge.blue()},{edge.alpha()});"
            " border-radius: 10px; }"
        )

    # --- expanding ----------------------------------------------------------

    @property
    def expanded(self) -> bool:
        # isHidden, not isVisible: isVisible() is False for every widget whose
        # window has not been shown yet, so this could not answer for a card
        # that had been expanded before the window came up.
        return not self.graph_load.isHidden()

    def set_expanded(self, expanded: bool) -> None:
        """History instead of the bars, not as well as them.

        The two say the same thing, and the right-hand end of the graph is the
        bar -- so showing both is one reading twice, in a card that then needs
        twice the height.
        """
        self.graph_load.setVisible(expanded)
        self.graph_memory.setVisible(expanded)
        self.meter_load.setVisible(not expanded)
        self.meter_memory.setVisible(not expanded)

    # --- what a sample changes ----------------------------------------------

    def show_stats(self, stats: host_stats.HostStats) -> None:
        self.setToolTip(stats.summary)
        if not stats.ok:
            self.show_error(stats.summary)
            return
        cores = f"{stats.cores} cores" if stats.cores else ""
        if stats.threads > stats.cores > 0:
            # Named separately: a user who knows the machine as "12 threads"
            # should see why the bar is scaled to 8 rather than assume it is
            # broken.
            cores += f", {stats.threads} threads"
        self.lbl_state.setText(cores)

        # The percentage first, because that is the comparable number: a load
        # of 8 means nothing until you know whether the machine has four cores
        # or sixty-four. The raw figures follow it for anyone who wants them.
        load = stats.load[0] if stats.load else 0.0
        if stats.cores:
            self.meter_load.show_value(
                stats.load_fraction,
                f"{stats.load_fraction * 100:.0f}%",
                f"of {stats.cores} cores",
                f"load {load:.2f} of {stats.cores} cores",
            )
        else:
            self.meter_load.show_value(
                0.0, f"{load:.2f}", "load", "the host did not report its cores"
            )

        total = f"{stats.mem_total_mb / 1024:.1f} GB" if stats.mem_total_mb else ""
        if stats.mem_total_mb and stats.mem_free_mb:
            self.meter_memory.show_value(
                stats.memory_fraction,
                f"{stats.memory_fraction * 100:.0f}%",
                f"of {total}",
                f"{stats.mem_used_mb / 1024:.1f} of {total} in use",
            )
        elif stats.mem_total_mb:
            # The host has no MemAvailable to report, so the bar would be a
            # guess -- but the size of the machine is still worth saying.
            self.meter_memory.show_value(
                0.0, "-", f"of {total}", f"{total} total, usage not reported"
            )
        else:
            self.meter_memory.show_value(
                0.0, "-", "memory", "the host did not report its memory"
            )

        self.graph_load.add(stats.load_fraction)
        self.graph_memory.add(stats.memory_fraction)

    def show_error(self, message: str) -> None:
        first = message.splitlines()[0] if message else "no answer"
        self.lbl_state.setText(first)
        self.setToolTip(first)
        self.meter_load.show_value(0.0, "-")
        self.meter_memory.show_value(0.0, "-")


class HostMonitorDialog(QDialog):
    """A card per host, refreshed on a timer while this window is open."""

    #: One card fits comfortably in this much width. Fewer, wider columns are
    #: better than many cramped ones: a bar too short to read is worse than a
    #: second row.
    CARD_WIDTH = 320

    def __init__(self, service, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Hosts at work")
        make_independent(self)
        # Wide enough for two columns of cards from the start: one column looks
        # like a list of three things, and two is where the layout reads as a
        # panel you can compare machines across.
        self.resize(2 * self.CARD_WIDTH + 60, 560)
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
        if self.btn_dark.isChecked():
            self._set_dark(True)
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
            self.cards[host.id] = HostCard(host)
        if not self.cards:
            self.grid.addWidget(QLabel("No hosts yet. Add one under Hosts..."), 0, 0)
        self._relayout()

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        layout.addWidget(box)



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
        """Apply the cadence and remember it."""
        self._timer.setInterval(int(seconds) * 1000)
        self.service.store.set_pref("host_monitor_interval", int(seconds))

    def _set_history(self, shown: bool) -> None:
        """Open or close the graphs on every card at once."""
        for card in self.cards.values():
            card.set_expanded(shown)
        self.service.store.set_pref("host_monitor_history", bool(shown))

    def _set_dark(self, dark: bool) -> None:
        """Repaint this window, and remember the choice.

        The palette goes on the window and nowhere else. Qt gives it to every
        child that has not been given one of its own, so assigning it to each
        of them by hand -- which an earlier version did -- only broke that
        inheritance, and turning the toggle off then left the window wearing a
        palette assembled from two.
        """
        self.setPalette(dark_palette(self._light_palette) if dark else self._light_palette)
        # The window and the scroll area's viewport both paint their own
        # background, so both have to be told to use the new one.
        self.setAutoFillBackground(True)
        if self._scroll is not None:
            self._scroll.viewport().setAutoFillBackground(True)
        for card in self.cards.values():
            card.restyle(self.palette())
        self.update()
        self.service.store.set_pref("host_monitor_dark", bool(dark))

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

        def work() -> str:
            transport = self._transport_for(host)
            # A floor of 15 s: connect_timeout is how long to wait for a
            # connection, and the probe also has to get through the login
            # files on a loaded machine before it can answer.
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

        # Quiet: a host that does not answer this tick is an ordinary outcome
        # here. It is shown on the card and backed off from, and a warning with
        # a traceback every few seconds would bury everything else in the log.
        run_async(self.service.pool, work, on_success=ok, on_error=failed, quiet=True)

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
