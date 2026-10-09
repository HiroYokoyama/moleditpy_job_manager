"""Live load and memory for every host, while the window is open.

Deliberately not part of polling. The sampling itself is
:mod:`.host_sampler`'s, shared with the web view; this window holds it while
it is open and draws what it reports.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenuBar,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION
from .host_sampler import (  # noqa: E402,F401 - re-exported for callers of this module
    DEFAULT_INTERVAL_SECONDS,
    MAX_BACKOFF_TICKS,
    OPENSSH_INTERVAL_SECONDS,
)
from .host_widgets import (
    _DARK as _DARK,
)
from .host_widgets import (
    _DARK_DIALOG_STYLE as _DARK_DIALOG_STYLE,
)
from .host_widgets import (
    _LIGHT_DIALOG_STYLE as _LIGHT_DIALOG_STYLE,
)
from .host_widgets import (
    BLANK as BLANK,
)
from .host_widgets import (
    GRAPH_CPU as GRAPH_CPU,
)
from .host_widgets import (
    GRAPH_LOAD as GRAPH_LOAD,
)
from .host_widgets import (
    GRAPH_MEMORY as GRAPH_MEMORY,
)
from .host_widgets import (
    HISTORY as HISTORY,
)
from .host_widgets import (
    HOURGLASS as HOURGLASS,
)
from .host_widgets import (
    NOT_SAMPLED as NOT_SAMPLED,
)
from .host_widgets import (
    HostCard as HostCard,
)
from .host_widgets import (
    Meter as Meter,
)
from .host_widgets import (
    Sparkline as Sparkline,
)
from .host_widgets import (
    _ActiveJobsBar as _ActiveJobsBar,
)
from .host_widgets import (
    _FixedLine as _FixedLine,
)
from .host_widgets import (
    dark_palette as dark_palette,
)
from .host_widgets import (
    primary_state_word as primary_state_word,
)
from .ui_actions import action_button, make_action, populate_menu
from .window_utils import make_independent


class HostMonitorDialog(QDialog):
    """A card per host, refreshed on a timer while this window is open."""

    #: What :func:`find_open` looks for.
    is_host_monitor = True

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
        #: Set by :meth:`_teardown`, which several close routes reach.
        self._torn_down = False
        from . import host_sampler

        #: Shared with the web view: see :mod:`.host_sampler`.
        self.sampler = host_sampler.for_service(service)
        self._build_ui()
        # Blocked: setValue emits valueChanged, which would record the
        # backend's default as the user's own choice on open.
        self.spin_interval.blockSignals(True)
        self.spin_interval.setValue(self.sampler.interval_seconds())
        self.spin_interval.blockSignals(False)
        if self.action_history.isChecked():
            self._set_history(True)
        # `setChecked` above happens before the signal connection, so it
        # never emitted `toggled`; apply the style explicitly here instead.
        self._set_dark(bool(self.action_dark.isChecked()))
        self.sampler.sampled.connect(self._on_sampled)
        self.sampler.sample_failed.connect(self._on_sample_failed)
        self.sampler.ticking.connect(self._sync_cards)
        # What the web view had sampled before this window opened is shown at
        # once rather than after the next round.
        for host_id, stats in list(self.sampler._latest.items()):
            if host_id in self.cards and not stats.error:
                self.cards[host_id].show_stats(stats)
        # The first holder gets a sample from acquire(); joining one already
        # running asks again now rather than leaving this window blank until
        # the next tick.
        joining = self.sampler.active
        self.sampler.acquire(self)
        if joining:
            self._sample_all()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._build_controls(layout)

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
        footer = QHBoxLayout()
        footer.addWidget(self._jobs_bar, 1)
        footer.addWidget(box)
        layout.addLayout(footer)

    def _build_controls(self, layout: QVBoxLayout) -> None:
        self.action_refresh = make_action(
            self,
            "Refresh Now",
            self._sample_all,
            "Sample the enabled hosts now.",
            shortcut="F5",
        )
        self.action_history = make_action(
            self,
            "Show History",
            self._set_history,
            "Show the last two minutes under every card: load in green, memory in blue.",
            checked=bool(self.service.store.get_pref("host_monitor_history", False)),
        )
        self.action_dark = make_action(
            self,
            "Dark Colours",
            self._set_dark,
            "Dark colours for this window only; MoleditPy's own theme is not touched.",
            checked=bool(self.service.store.get_pref("host_monitor_dark", False)),
        )
        self.action_close = make_action(self, "Close", self.reject, shortcut="Ctrl+W")
        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        layout.setMenuBar(self.menu_bar)
        populate_menu(
            self.menu_bar.addMenu("&Monitor"), (self.action_refresh, None, self.action_close)
        )
        populate_menu(self.menu_bar.addMenu("&View"), (self.action_history, self.action_dark))
        top = QHBoxLayout()
        self.btn_refresh = action_button(self.action_refresh, self)
        top.addWidget(self.btn_refresh)
        top.addStretch(1)
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
        layout.addLayout(top)

    def _disconnect_signals(self) -> None:
        """Disconnect service signals to prevent memory leaks on re-open."""
        if getattr(self, "_refresh_pending", None) is not None:
            self._refresh_pending.stop()
        for signal, slot in (
            (self.service.jobs_changed, self._request_card_refresh),
            (self.service.job_updated, self._on_job_updated),
            (self.sampler.sampled, self._on_sampled),
            (self.sampler.sample_failed, self._on_sample_failed),
            (self.sampler.ticking, self._sync_cards),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                # Already disconnected; teardown is allowed to run twice.
                logging.debug("Job Manager: host monitor signal already disconnected")
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
            (
                host.id,
                host.name,
                host.target,
                bool(host.enabled),
                bool(host.monitor_usage),
                int(host.monitor_interval or 0),
            )
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
            if not host.enabled:
                card.setEnabled(False)
                card.lbl_state.setText("disabled")
                # HostCard paints with fixed colours, not the palette, so
                # setEnabled() alone would look identical to an enabled card.
                effect = QGraphicsOpacityEffect(card)
                effect.setOpacity(0.45)
                card.setGraphicsEffect(effect)
            elif not host.monitor_usage:
                card.show_not_sampled()
            card.restyle(self.palette(), dark=bool(self.action_dark.isChecked()))
            self.cards[host.id] = card
        self._empty_label.setVisible(not self.cards)
        self._relayout()
        self._refresh_card_jobs()
        if hasattr(self, "sampler"):
            self.sampler.reschedule()

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
        self.sampler.reschedule()

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

    def _sample_all(self) -> None:
        """Ask now, rather than at the next tick. The sampler re-reads the
        host list first, which is what rebuilds the cards."""
        self.sampler.sample_all()

    def _on_sampled(self, host_id: str, stats) -> None:
        card = self.cards.get(host_id)
        if card is not None:
            card.show_stats(stats)

    def _on_sample_failed(self, host_id: str, message: str, retry_seconds: int) -> None:
        card = self.cards.get(host_id)
        if card is None:
            return
        if retry_seconds:
            card.show_error(f"{message} - retrying in {retry_seconds}s")
        else:
            card.show_error(message)

    # --- teardown -----------------------------------------------------------

    def _save_settings(self) -> None:
        """Save user preferences for Host Monitor only upon closing."""
        try:
            self.service.store.set_pref("host_monitor_interval", int(self.spin_interval.value()))
            self.service.store.set_pref(
                "host_monitor_history", bool(self.action_history.isChecked())
            )
            self.service.store.set_pref("host_monitor_dark", bool(self.action_dark.isChecked()))
        except (OSError, ValueError, TypeError):
            logging.warning("Job Manager: the host monitor settings were not saved", exc_info=True)

    def _teardown(self) -> None:
        """Stop sampling, save settings, and hand every connection back.

        Written once and guarded, rather than repeated in each of the four ways
        a dialog closes. Every route used to carry its own copy, and since
        closeEvent() calls reject() and both reject() and accept() call done(),
        closing a visible window ran the whole sequence three times: three
        settings writes, three disconnect sweeps, three passes over the open
        transports.
        """
        if self._torn_down:
            return
        self._torn_down = True
        # Released, not stopped: the web view may still be holding it, and the
        # sampler hands the connections back when the last holder lets go.
        self.sampler.release(self)
        self._save_settings()
        self._disconnect_signals()

    def done(self, r: int) -> None:
        # Where accept(), reject() and a close on a *visible* window all arrive.
        self._teardown()
        super().done(r)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        # And the one case done() does not cover: QDialog::closeEvent only
        # calls reject() when the dialog is visible, so a window closed without
        # ever being shown would otherwise keep its timer -- and go on sampling
        # every host over SSH for the rest of the session, which is the one
        # thing this window promises not to do.
        self._teardown()
        # Delegated, not accepted: this is what reaches reject() and so emits
        # ``finished``, which is what deregisters the window key.
        super().closeEvent(event)


def find_open(service) -> Optional[HostMonitorDialog]:
    """The Host Monitor already on screen for ``service``, if any.

    Without MoleditPy there is no window registry: the tray's Host Monitor
    and the job monitor's button each opened one of their own.
    """
    app = QApplication.instance()
    for widget in app.topLevelWidgets() if app is not None else []:
        # By marker, not isinstance: the class is replaced in tests.
        if (
            getattr(widget, "is_host_monitor", False)
            and getattr(widget, "service", None) is service
            and widget.isVisible()
            and not widget._torn_down
        ):
            return widget
    return None


__all__ = ["HostCard", "HostMonitorDialog", "Sparkline", "find_open"]
