"""Live load and memory for every host, while the window is open.

Deliberately not part of polling: sampling stops the moment this window
closes, so a Job Manager left open overnight touches no login node. The
transport is held open per host for the same reason -- rebuilding a
connection every two seconds would cost more than the measurement.
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
from .models import (
    ACTIVE_STATES,
    SCHEDULER_WINDOWS,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_LOST,
    STATE_NEW,
    STATE_RUNNING,
    STATE_UPLOADING,
    HostProfile,
)
from .theme import (
    CY_ACCENT,
    CY_ACCENT2,
    CY_AMBER,
    CY_GREEN,
    CY_GREY,
    CY_RED,
)

from .window_utils import make_independent
from .tasks import run_async

#: How many samples a graph keeps. At the default interval that is about two
#: minutes of history, which is enough to see a job start.
HISTORY = 60

#: Two seconds suits a backend that keeps its connection (paramiko, local).
#: OpenSSH spawns a fresh ssh process per command -- a fresh TCP connect,
#: handshake and auth every tick -- and a burst of them trips sshd's own
#: connection throttling, which shows up here as a timeout on a healthy host.
DEFAULT_INTERVAL_SECONDS = 2
OPENSSH_INTERVAL_SECONDS = 10

#: A host that fails is asked less often, doubling up to this, rather than
#: every tick for as long as the window is open.
MAX_BACKOFF_TICKS = 16

#: A real space that keeps a label its full height while it has nothing to
#: say. Written as the character, never ``&nbsp;``: QLabel's AutoText format
#: does not recognise that entity as markup, so it showed the literal text.
BLANK = "\u00a0"

#: Waiting, drawn as the *white hourglass* rather than the emoji one. The
#: emoji codepoint (U+231B) renders as a crushed, off-baseline colour glyph;
#: U+29D6 is a math symbol drawn by the text font at the text size instead.
HOURGLASS = "\u29d6"

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


#: One colour per thing measured, shared by the bar and the graph under it:
#: load green, memory blue. A bar that changed hue with its value was harder
#: to compare across cards.
GRAPH_CPU = QColor(CY_GREEN)
GRAPH_LOAD = GRAPH_CPU
GRAPH_MEMORY = QColor(CY_ACCENT2)


def primary_state_word(job) -> str:
    """What a card calls a job that is not active any more: a lowercase word,
    not the state name -- "FAILED" beside a machine name reads as the
    machine having failed."""
    return {
        STATE_DONE: "finished",
        STATE_FAILED: "failed",
        STATE_CANCELLED: "cancelled",
        STATE_LOST: "lost",
    }.get(job.state, (job.state or "").lower())


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
        # The bar is the thing this card exists to show, so it takes whatever
        # height the card has spare.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        # Tall enough to compare across cards at a glance, short enough that
        # two cards still fit the window at once.
        return QSize(100, 140)

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
    """History graph displaying samples over time.

    The top of the plot is always 100% of the machine, never the largest
    value seen -- a graph that rescaled itself would make a quiet host look
    as busy as a full one. The ceiling is drawn and labelled, not implied.
    """

    def __init__(self, color: QColor, caption: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.color = color
        self.caption = caption
        self.values: Deque[float] = deque(maxlen=HISTORY)
        self._dark = False
        # Room for the label row above the plot as well as the plot itself;
        # a tighter minimum left the plot barely taller than its caption.
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(180, 128)

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
        # The plot area starts below the labels, so the 100% line sits at the
        # actual ceiling rather than under the caption text.
        outer = self.rect().adjusted(0, 1, -1, -1)
        label_height = painter.fontMetrics().height()
        rect = outer.adjusted(0, label_height + 1, 0, 0)

        guide_color = QColor("#8b949e" if self._dark else "#656d76")

        guide = QColor(guide_color)
        guide.setAlpha(60)
        painter.setPen(QPen(guide, 1.0, Qt.PenStyle.DotLine))
        for share in (0.25, 0.5, 0.75):
            y = int(rect.bottom() - share * rect.height())
            painter.drawLine(rect.left() + 2, y, rect.right() - 2, y)
        # The ceiling, solid and a shade stronger than the quarter marks.
        ceiling = QColor(guide_color)
        ceiling.setAlpha(110)
        painter.setPen(QPen(ceiling, 1.0))
        painter.drawLine(rect.left() + 2, rect.top(), rect.right() - 2, rect.top())

        header = outer.adjusted(6, 0, -6, -(outer.height() - label_height))
        painter.setPen(QPen(guide_color))
        if self.caption:
            current = f"  {self.values[-1] * 100:.0f}%" if self.values else ""
            painter.drawText(
                header,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                f"{self.caption}{current}",
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


class _FixedLine(QLabel):
    """Exactly one line of text, whatever it says.

    The card grid stops being uniform the moment a long job name wraps onto a
    second line. This never wraps and takes no part in deciding card width;
    text too long for the width is elided instead.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWordWrap(False)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.fontMetrics().height() + 2)
        self._head = ""
        self._tail = ""
        self._head_style = ""
        self._tail_style = ""
        self.clear_line()

    def setFont(self, font) -> None:  # noqa: N802 - Qt's spelling
        super().setFont(font)
        self.setFixedHeight(self.fontMetrics().height() + 2)

    def show_line(
        self, head: str, tail: str = "", head_style: str = "", tail_style: str = ""
    ) -> None:
        """``head`` is elided to fit; ``tail`` is kept whole beside it."""
        self._head, self._tail = head, tail
        self._head_style, self._tail_style = head_style, tail_style
        self._render()

    def clear_line(self) -> None:
        self.show_line("")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        super().resizeEvent(event)
        self._render()

    def _render(self) -> None:
        import html as _html

        if not self._head and not self._tail:
            self.setText(BLANK)
            return
        metrics = self.fontMetrics()
        room = self.width() - metrics.horizontalAdvance(self._tail) - 4
        # Only once the layout has given this label a real width -- Qt's
        # default 100px would elide down to nothing.
        laid_out = self.testAttribute(Qt.WidgetAttribute.WA_Resized)
        head = (
            metrics.elidedText(self._head, Qt.TextElideMode.ElideRight, room)
            if laid_out and room > 20
            else self._head
        )
        parts = [f"<span style='{self._head_style}'>{_html.escape(head)}</span>"]
        if self._tail:
            parts.append(f"<span style='{self._tail_style}'>{_html.escape(self._tail)}</span>")
        self.setText("".join(parts))


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

        #: 1-minute load average, a coarser second reading beside the target.
        self.lbl_load_avg = QLabel("")
        font = self.lbl_load_avg.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() * 0.85))
        self.lbl_load_avg.setFont(font)
        self.lbl_load_avg.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_load_avg.setText(BLANK)
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
        outer.addLayout(columns, 1)
        self._outer = outer
        self._meter_row = columns

        self.graph_cpu = Sparkline(GRAPH_CPU, "CPU")
        self.graph_load = self.graph_cpu
        self.graph_memory = Sparkline(GRAPH_MEMORY, "memory")
        for widget in (self.graph_cpu, self.graph_memory):
            widget.setVisible(False)
            outer.addWidget(widget, 1)

        # One line for the job, one for the counts -- two labels, never one
        # wrapping label, or a long job name would push the counts out of the
        # card's fixed height.
        self._jobs: list = []
        self.lbl_job = _FixedLine(self)
        self.lbl_job_counts = _FixedLine(self)
        counts_font = self.lbl_job_counts.font()
        counts_font.setPointSizeF(max(7.5, counts_font.pointSizeF() * 0.85))
        self.lbl_job_counts.setFont(counts_font)
        outer.addWidget(self.lbl_job)
        outer.addWidget(self.lbl_job_counts)

        self.restyle()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt's spelling
        """Never shorter than the card asked to be (height only -- width still
        shrinks, since the labels elide).

        The scroll area shrinks its contents to their minimum before showing
        a scrollbar, which squeezed the bars to a sliver on a short window.
        """
        hint = super().minimumSizeHint()
        return QSize(hint.width(), max(hint.height(), self.sizeHint().height()))

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
        # A hidden widget drops out of the layout, but the nested row holding
        # it does not: left with a stretch factor it would still claim height
        # for two invisible bars.
        self._outer.setStretchFactor(self._meter_row, 0 if expanded else 1)

    def show_jobs(self, jobs: list) -> None:
        """One line for what this host is doing, one for how far it has got.

        The named job is chosen by state, not list order: whatever is
        running, else most recently submitted, else the last to finish.
        Matching only RUNNING left a card blank for the whole of a
        resubmission, since a handed-over job starts in NEW/UPLOADING first.
        """
        self._jobs = list(jobs)
        self._render_jobs()

    def _render_jobs(self) -> None:
        jobs = self._jobs
        if not jobs:
            self.lbl_job.clear_line()
            self.lbl_job_counts.clear_line()
            return

        total = len(jobs)
        # Anything that has stopped for good, however it stopped -- the same
        # measure the summary bar reports. Counting only DONE undercounted a
        # host with failed/cancelled jobs as still having work to come.
        finished = [job for job in jobs if job.is_terminal]
        # Cancelled is not a failure -- the user stopped it themselves.
        failed = [job for job in finished if job.state in (STATE_FAILED, STATE_LOST)]
        # Every non-terminal state, not a hand-listed few: DOWNLOADING used
        # to fall through both halves and vanish from the card.
        unfinished = [job for job in jobs if not job.is_terminal]
        starting = [job for job in jobs if job.state in (STATE_NEW, STATE_UPLOADING)]
        running = [job for job in jobs if job.state == STATE_RUNNING]
        waiting = [job for job in jobs if job.state in ACTIVE_STATES and job.state != STATE_RUNNING]
        fetching = [job for job in jobs if job.state == STATE_DOWNLOADING]

        def latest(candidates: list):
            # By handover time, never updated_at: a poll rewrites that on
            # every job it touches. Ties (a same-second batch, Windows'
            # coarse clock) are broken by id, arbitrarily but stably.
            return max(candidates, key=lambda job: (job.started_at or job.submitted_at, job.id))

        if running:
            primary, word, color, mark = latest(running), "running", CY_GREEN, ""
        elif starting:
            primary, word, color, mark = latest(starting), "submitting", CY_AMBER, HOURGLASS
        elif waiting:
            primary, word, color, mark = latest(waiting), "queued", CY_AMBER, HOURGLASS
        elif fetching:
            primary, word, color, mark = latest(fetching), "downloading", CY_AMBER, ""
        else:
            last = latest(jobs)
            primary, word, color, mark = last, primary_state_word(last), CY_GREY, ""

        name = f"{mark} {primary.name}" if mark else (primary.name or "job")
        self.lbl_job.show_line(
            name,
            f" - {word}",
            head_style=f"color:{color};font-weight:bold",
            tail_style=f"color:{CY_GREY}",
        )
        counts = [f"{len(unfinished)} active"] if unfinished else []
        counts.append(f"{len(finished)}/{total} finished")
        if failed:
            counts.append(f"{len(failed)} failed")
        self.lbl_job_counts.show_line(", ".join(counts), head_style=f"color:{CY_GREY}")

    # --- what a sample changes ----------------------------------------------

    def show_stats(self, stats: host_stats.HostStats) -> None:
        self.setToolTip(stats.summary)
        if not stats.ok:
            self.show_error(stats.summary)
            return
        # load_fraction is computed against thread (logical CPU) count, not
        # the physical core count.
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
            self.lbl_load_avg.setToolTip("1-minute load average, as the host reports it.")
        else:
            self.lbl_load_avg.setText(BLANK)
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
        self.lbl_load_avg.setText(BLANK)
        self.lbl_load_avg.setToolTip("")


class HostMonitorDialog(QDialog):
    """A card per host, refreshed on a timer while this window is open."""

    #: One card fits comfortably in this much width; fewer wide columns beat
    #: many cramped ones.
    CARD_WIDTH = 320

    def __init__(self, service, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Host Monitor")
        make_independent(self)
        # Wide enough for two columns from the start: one column reads as a
        # list, two as a panel you can compare machines across.
        self.resize(2 * self.CARD_WIDTH + 60, 660)
        self.cards: Dict[str, HostCard] = {}
        #: The palette this window was born with, so the dark toggle has
        #: something exact to go back to.
        self._light_palette = QPalette(self.palette())
        self._scroll: Optional[QScrollArea] = None
        #: Held open while this window is: see the module docstring.
        self._transports: Dict[str, object] = {}
        #: Hosts with a probe still in flight, so a slow host does not queue
        #: up one worker per tick.
        self._busy: set = set()
        #: Ticks still to skip for a host that failed, and the size of the
        #: skip it earned. Both cleared by a sample that works.
        self._skip_ticks: Dict[str, int] = {}
        self._backoff: Dict[str, int] = {}
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sample_all)
        # The stored choice wins; the per-backend default is only a starting
        # point, not a correction applied over the top of it.
        stored = int(self.service.store.get_pref("host_monitor_interval", 0) or 0)
        # Blocked: setValue emits valueChanged, which would record the
        # backend's default as the user's own choice on open.
        self.spin_interval.blockSignals(True)
        self.spin_interval.setValue(stored or self._default_interval())
        self.spin_interval.blockSignals(False)
        self._timer.start(self.spin_interval.value() * 1000)
        if self.btn_history.isChecked():
            self._set_history(True)
        # `setChecked` above happens before the signal connection, so it
        # never emitted `toggled`; apply the style explicitly here instead.
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
            "How often each host is asked, while this window is open. Remembered."
        )
        self.spin_interval.valueChanged.connect(self._set_interval)
        top.addWidget(self.spin_interval)
        top.addStretch(1)
        self.btn_history = QPushButton("History")
        self.btn_history.setCheckable(True)
        self.btn_history.setToolTip(
            "Show the last two minutes under every card: load in green, memory in blue."
        )
        self.btn_history.setChecked(
            bool(self.service.store.get_pref("host_monitor_history", False))
        )
        self.btn_history.toggled.connect(self._set_history)
        top.addWidget(self.btn_history)

        self.btn_dark = QPushButton("Dark")
        self.btn_dark.setCheckable(True)
        self.btn_dark.setToolTip(
            "Dark colours for this window only; MoleditPy's own theme is not touched."
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

        self._empty_label = QLabel("No hosts yet. Add one under Hosts...")
        self.grid.addWidget(self._empty_label, 0, 0)
        self._build_cards()
        self._refresh_pending = QTimer(self)
        self._refresh_pending.setSingleShot(True)
        self._refresh_pending.setInterval(120)
        self._refresh_pending.timeout.connect(self._refresh_card_jobs)
        self.service.jobs_changed.connect(self._request_card_refresh)
        self.service.job_updated.connect(self._on_job_updated)
        self._refresh_card_jobs()

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)

        self._jobs_bar = _ActiveJobsBar(self.service)
        layout.addWidget(self._jobs_bar)
        layout.addWidget(box)

    def _disconnect_signals(self) -> None:
        """Disconnect service signals to prevent memory leaks on re-open."""
        if getattr(self, "_refresh_pending", None) is not None:
            self._refresh_pending.stop()
        try:
            self.service.jobs_changed.disconnect(self._request_card_refresh)
        except Exception:
            pass
        try:
            self.service.job_updated.disconnect(self._on_job_updated)
        except Exception:
            pass
        if hasattr(self, "_jobs_bar") and self._jobs_bar is not None:
            self._jobs_bar.teardown()

    def _on_job_updated(self, _job_id: str = "") -> None:
        self._request_card_refresh()

    def _request_card_refresh(self) -> None:
        """Coalesce a burst of job signals into one pass over the cards (a
        poll resolving eight jobs used to walk the whole list eight times)."""
        if not self._refresh_pending.isActive():
            self._refresh_pending.start()

    def _refresh_card_jobs(self) -> None:
        """Push each host's jobs into its card's strip.

        Grouped by id and by name into separate maps: one map keyed by both
        risked a host's *name* colliding with another host's *id*, and a job
        double-counted on a card that fell back to the name.
        """
        by_id: Dict[str, list] = {}
        by_name: Dict[str, list] = {}
        for job in self.service.store.jobs.values():
            if job.host_id:
                by_id.setdefault(job.host_id, []).append(job)
            elif job.host_name:
                # Fallback only: a job with a host id is placed by that alone,
                # so renaming a host cannot split it.
                by_name.setdefault(job.host_name, []).append(job)
        for card in self.cards.values():
            card.show_jobs(by_id.get(card.host.id) or by_name.get(card.host.name, []))

    def _host_signature(self) -> tuple:
        """What the cards depend on: which hosts there are, and their labels."""
        return tuple(
            (host.id, host.name, host.target, bool(getattr(host, "enabled", True)))
            for host in self.service.store.host_list()
        )

    def _build_cards(self) -> None:
        """Make one card per host, replacing whatever was there. Called again
        whenever the host list changes underneath the window."""
        self._card_signature = self._host_signature()
        for card in self.cards.values():
            self.grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self.cards.clear()
        self._laid_out_for = 0
        for host in self.service.store.host_list():
            card = HostCard(host)
            if not getattr(host, "enabled", True):
                card.setEnabled(False)
                card.lbl_state.setText("disabled")
                # HostCard paints with fixed colours, not the palette, so
                # setEnabled() alone would look identical to an enabled card.
                effect = QGraphicsOpacityEffect(card)
                effect.setOpacity(0.45)
                card.setGraphicsEffect(effect)
            card.restyle(self.palette(), dark=bool(self.btn_dark.isChecked()))
            self.cards[host.id] = card
        self._empty_label.setVisible(not self.cards)
        self._relayout()
        self._refresh_card_jobs()

    def _sync_cards(self) -> None:
        """Rebuild the cards if the host list has changed since they were made."""
        if self._host_signature() != self._card_signature:
            self._build_cards()

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
            # Spare height goes after the last row of cards, not into them.
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

        # Both branches set an explicit stylesheet: an empty one for "light"
        # left native chrome with different padding than dark mode's own,
        # so toggling visibly changed widget sizes.
        self.setStyleSheet(_DARK_DIALOG_STYLE if dark else _LIGHT_DIALOG_STYLE)
        self.setAutoFillBackground(True)
        if self._scroll is not None:
            self._scroll.viewport().setAutoFillBackground(True)
            self._scroll.viewport().setPalette(pal)
            if hasattr(self, "body") and self.body is not None:
                self.body.setPalette(pal)
        # Qt caches each widget's resolved style; setStyleSheet() alone does
        # not always invalidate children that already painted, so a toggle
        # could look "stuck". unpolish/polish forces a recompute.
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        for child in self.findChildren(QWidget):
            style.unpolish(child)
            style.polish(child)
            child.update()
        # polish() can synthesize palette roles from the stylesheet's own
        # background-color, silently drifting the palette away from ``pal``;
        # setting it again after polish is what actually makes it win.
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
        # Cheap tuple comparison; there is no store signal for host edits.
        self._sync_cards()
        for host in self._hosts():
            if host.id in self._busy:
                # Still waiting on the last probe; stacking more would only
                # make a slow host slower.
                continue
            waiting = self._skip_ticks.get(host.id, 0)
            if waiting:
                # Backing off after a failure, so as not to hammer an
                # unreachable host every tick.
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
        # Resolved on the GUI thread: concurrent workers reading/writing
        # self._transports without a lock would be a data race.
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
            # A failed probe drops the connection, so the next tick builds a
            # new one instead of reusing an already-closed socket.
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
        """Hand a transport's teardown to the pool instead of closing it here:
        paramiko's close() can block on a host that has gone quiet. Popped
        from ``self._transports`` immediately either way, so a probe stops
        seeing it as open the moment this returns."""
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

        # Coalesced for the same reason the cards are: one poll emits a
        # signal per job resolved.
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(120)
        self._pending.timeout.connect(self.refresh)
        service.jobs_changed.connect(self._request_refresh)
        service.job_updated.connect(self._on_updated)
        self.refresh()

    def _request_refresh(self) -> None:
        if not self._pending.isActive():
            self._pending.start()

    def teardown(self) -> None:
        """Disconnect service signals; call when the parent dialog closes."""
        self._pending.stop()
        try:
            self.service.jobs_changed.disconnect(self._request_refresh)
        except Exception:
            pass
        try:
            self.service.job_updated.disconnect(self._on_updated)
        except Exception:
            pass

    def _on_updated(self, _job_id: str = "") -> None:
        self._request_refresh()

    def refresh(self) -> None:
        store = self.service.store
        active = store.active_jobs()
        if not active:
            self._lbl_count.setText(f"<span style='color:{CY_GREY};'>no active jobs</span>")
            return

        blocked_ids = store.blocked_ids()
        running = sum(
            1 for job in active if job.state == STATE_RUNNING and job.id not in blocked_ids
        )
        blocked = sum(1 for job in active if job.id in blocked_ids)
        waiting = len(active) - running - blocked
        all_jobs = list(store.jobs.values())
        finished = sum(1 for job in all_jobs if job.is_terminal)

        parts = []
        if running:
            parts.append(
                f"<span style='color:{CY_GREEN};font-weight:bold'>{running} running</span>"
            )
        if waiting:
            parts.append(f"<span style='color:{CY_AMBER};'>{HOURGLASS} {waiting} waiting</span>")
        if blocked:
            parts.append(f"<span style='color:{CY_RED};'>{blocked} blocked</span>")
        parts.append(f"<span style='color:{CY_GREY};'>{finished}/{len(all_jobs)} finished</span>")
        self._lbl_count.setText("&nbsp;&nbsp;".join(parts))


__all__ = ["HostCard", "HostMonitorDialog", "Sparkline"]
