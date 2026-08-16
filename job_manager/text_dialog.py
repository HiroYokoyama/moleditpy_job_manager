"""A plain read-only text window, used for the log tail and the job details.

Both were shown in the strip at the bottom of the monitor, which is four lines
tall and shared with every status message: a two-hundred-line log arrived, and
the part worth reading had already scrolled past. A window can be resized, kept
open beside the table, and read while the list keeps updating behind it.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent, QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .theme import apply_theme
from .window_utils import make_independent


class TextDialog(QDialog):
    """Read-only monospaced text, with an optional Refresh and Auto-refresh timer."""

    def __init__(
        self,
        title: str,
        text: str = "",
        parent: Optional[QWidget] = None,
        on_refresh: Optional[Callable[[], None]] = None,
        auto_interval: int = 5,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        make_independent(self)
        apply_theme(self)
        self.resize(820, 520)
        self._on_refresh_callback = on_refresh

        layout = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        # The system's own fixed-width face: a log is columns, and a proportional
        # font turns a queue listing into a mess.
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setPlainText(text)
        layout.addWidget(self.view, 1)

        bottom_row = QHBoxLayout()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._trigger_auto_refresh)

        if on_refresh is not None:
            self.chk_auto_refresh = QCheckBox("Auto-refresh")
            self.chk_auto_refresh.setToolTip("Periodically refresh the log tail while this window is open.")
            self.spin_interval = QSpinBox()
            self.spin_interval.setRange(1, 120)
            self.spin_interval.setValue(max(1, auto_interval))
            self.spin_interval.setSuffix(" s")
            self.spin_interval.setToolTip("Refresh interval in seconds.")
            self.lbl_interval = QLabel("every")

            self.chk_auto_refresh.toggled.connect(self._on_auto_refresh_toggled)
            self.spin_interval.valueChanged.connect(self._on_interval_changed)
            self.chk_auto_refresh.setChecked(True)

            bottom_row.addWidget(self.chk_auto_refresh)
            bottom_row.addWidget(self.lbl_interval)
            bottom_row.addWidget(self.spin_interval)
            bottom_row.addSpacing(12)


        bottom_row.addStretch(1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if on_refresh is not None:
            self.btn_refresh = QPushButton("Refresh")
            self.btn_refresh.clicked.connect(self._trigger_refresh)
            box.addButton(self.btn_refresh, QDialogButtonBox.ButtonRole.ActionRole)
        # Close has RejectRole, so the box emits rejected for it. Connecting
        # its clicked as well would call reject() twice and emit finished twice.
        box.rejected.connect(self.reject)
        bottom_row.addWidget(box)
        layout.addLayout(bottom_row)

    def _trigger_refresh(self) -> None:
        if self._on_refresh_callback is not None:
            self._on_refresh_callback()

    def _trigger_auto_refresh(self) -> None:
        if self.isVisible() and self._on_refresh_callback is not None:
            self._on_refresh_callback()

    def _on_auto_refresh_toggled(self, checked: bool) -> None:
        if checked and self._on_refresh_callback is not None:
            self._timer.start(int(self.spin_interval.value() * 1000))
        else:
            self._timer.stop()

    def _on_interval_changed(self, value: int) -> None:
        if hasattr(self, "chk_auto_refresh") and self.chk_auto_refresh.isChecked():
            self._timer.start(int(value * 1000))

    def showEvent(self, event) -> None:  # noqa: N802
        """Start auto-refresh when the window becomes visible."""
        super().showEvent(event)
        if (
            hasattr(self, "chk_auto_refresh")
            and self.chk_auto_refresh.isChecked()
            and self._on_refresh_callback is not None
        ):
            self._timer.start(int(self.spin_interval.value() * 1000))

    def hideEvent(self, event) -> None:  # noqa: N802
        """Pause auto-refresh while the window is hidden or minimized."""
        self._timer.stop()
        super().hideEvent(event)


    def set_text(self, text: str) -> None:
        """Replace the contents, keeping the view scrolled to the end.

        The end is where a log is written, so that is what a refresh should
        show without asking the reader to scroll for it every time.
        """
        self.view.setPlainText(text)
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._timer.stop()
        super().closeEvent(event)

    def reject(self) -> None:
        self._timer.stop()
        super().reject()

    def accept(self) -> None:
        self._timer.stop()
        super().accept()


__all__ = ["TextDialog"]

