"""A plain read-only text window, used for the log tail and the job details.

Both were shown in the strip at the bottom of the monitor, which is four lines
tall and shared with every status message: a two-hundred-line log arrived, and
the part worth reading had already scrolled past. A window can be resized, kept
open beside the table, and read while the list keeps updating behind it.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class TextDialog(QDialog):
    """Read-only monospaced text, with an optional Refresh."""

    def __init__(
        self,
        title: str,
        text: str = "",
        parent: Optional[QWidget] = None,
        on_refresh: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        # The system's own fixed-width face: a log is columns, and a proportional
        # font turns a queue listing into a mess.
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setPlainText(text)
        layout.addWidget(self.view, 1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if on_refresh is not None:
            self.btn_refresh = QPushButton("Refresh")
            self.btn_refresh.clicked.connect(lambda: on_refresh())
            box.addButton(self.btn_refresh, QDialogButtonBox.ButtonRole.ActionRole)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        layout.addWidget(box)

    def set_text(self, text: str) -> None:
        """Replace the contents, keeping the view scrolled to the end.

        The end is where a log is written, so that is what a refresh should
        show without asking the reader to scroll for it every time.
        """
        self.view.setPlainText(text)
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())


__all__ = ["TextDialog"]
