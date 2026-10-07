"""A plain read-only text window, used for the log tail and the job details.

Both were shown in the strip at the bottom of the monitor, which is four lines
tall and shared with every status message: a two-hundred-line log arrived, and
the part worth reading had already scrolled past. A window can be resized, kept
open beside the table, and read while the list keeps updating behind it.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QFontDatabase,
    QKeySequence,
    QShortcut,
    QTextCursor,
    QTextDocument,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .theme import apply_theme
from .window_utils import make_independent


class _FindEdit(QLineEdit):
    """The search field. Takes Enter and Esc for itself: in a dialog both
    otherwise reach the dialog, where Enter presses a button and Esc closes
    the whole window instead of just the search."""

    #: True to search backwards.
    search = pyqtSignal(bool)
    dismissed = pyqtSignal()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's spelling
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.search.emit(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.dismissed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class TextDialog(QDialog):
    """Read-only monospaced text, with an optional Refresh and Auto-refresh timer."""

    #: Preference names for the auto-refresh controls. One pair for every tail
    #: window: the question "how often do I want to see this" is about the user
    #: and their connection, not about the particular file.
    PREF_ENABLED = "tail_auto_refresh"
    PREF_INTERVAL = "tail_refresh_interval"

    def __init__(
        self,
        title: str,
        text: str = "",
        parent: Optional[QWidget] = None,
        on_refresh: Optional[Callable[[], None]] = None,
        auto_interval: int = 5,
        store: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        #: When given, the auto-refresh choice is remembered in it. Optional so
        #: this stays a plain text window for callers that have no store.
        self._store = store
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
        layout.addWidget(self._build_find_bar())

        bottom_row = QHBoxLayout()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._trigger_auto_refresh)

        if on_refresh is not None:
            self.chk_auto_refresh = QCheckBox("Auto-refresh")
            self.chk_auto_refresh.setToolTip(
                "Periodically refresh the log tail while this window is open."
            )
            self.spin_interval = QSpinBox()
            self.spin_interval.setRange(1, 120)
            self.spin_interval.setSuffix(" s")
            self.spin_interval.setToolTip("How often the file is read again.")
            self.lbl_interval = QLabel("every")

            # The stored choice wins over the per-backend suggestion, and is
            # applied before the signals are connected so restoring it is not
            # recorded as a change the user made.
            self.spin_interval.setValue(self._stored_interval(max(1, auto_interval)))
            self.chk_auto_refresh.setChecked(self._stored_enabled())
            self.chk_auto_refresh.toggled.connect(self._on_auto_refresh_toggled)
            self.spin_interval.valueChanged.connect(self._on_interval_changed)
            if self.chk_auto_refresh.isChecked():
                self._timer.start(int(self.spin_interval.value() * 1000))

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

    # --- find ------------------------------------------------------------------

    def _build_find_bar(self) -> QWidget:
        """Ctrl+F opens it; Enter / F3 finds the next match, with Shift the one
        before; Esc closes it. Hidden until asked for."""
        self.find_bar = QWidget()
        row = QHBoxLayout(self.find_bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Find"))
        self.txt_find = _FindEdit()
        self.txt_find.setPlaceholderText("Text to find (Enter: next, Shift+Enter: previous)")
        self.txt_find.setClearButtonEnabled(True)
        self.txt_find.search.connect(self.find)
        self.txt_find.dismissed.connect(self.hide_find)
        # Searching as it is typed, from where the last match began, so the
        # match grows with the word instead of jumping to the next one.
        self.txt_find.textEdited.connect(lambda _text: self.find(False, from_start_of_match=True))
        row.addWidget(self.txt_find, 1)
        self.chk_case = QCheckBox("Match case")
        row.addWidget(self.chk_case)
        self.btn_previous = QPushButton("Previous")
        self.btn_previous.setAutoDefault(False)
        self.btn_previous.clicked.connect(lambda: self.find(True))
        row.addWidget(self.btn_previous)
        self.btn_next = QPushButton("Next")
        self.btn_next.setAutoDefault(False)
        self.btn_next.clicked.connect(lambda: self.find(False))
        row.addWidget(self.btn_next)
        self.lbl_find = QLabel("")
        self.lbl_find.setStyleSheet("color: palette(mid);")
        row.addWidget(self.lbl_find)
        self.find_bar.setVisible(False)

        QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self, activated=self.show_find)
        QShortcut(QKeySequence("F3"), self, activated=lambda: self.find(False))
        QShortcut(QKeySequence("Shift+F3"), self, activated=lambda: self.find(True))
        return self.find_bar

    def show_find(self) -> None:
        # Whatever is selected in the text is what the user most likely wants.
        selected = self.view.textCursor().selectedText()
        if selected and "\u2029" not in selected:
            self.txt_find.setText(selected)
        self.find_bar.setVisible(True)
        self.txt_find.setFocus()
        self.txt_find.selectAll()

    def hide_find(self) -> None:
        self.find_bar.setVisible(False)
        self.lbl_find.setText("")
        self.view.setFocus()

    def find(self, backward: bool = False, from_start_of_match: bool = False) -> bool:
        """Select the next match, wrapping round the end. Returns whether found."""
        needle = self.txt_find.text()
        if not needle:
            self.lbl_find.setText("")
            return False
        if not self.find_bar.isVisible():
            self.find_bar.setVisible(True)
        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.chk_case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if from_start_of_match:
            cursor = self.view.textCursor()
            cursor.setPosition(cursor.selectionStart())
            self.view.setTextCursor(cursor)
        found = self.view.find(needle, flags)
        wrapped = False
        if not found:
            # Round the end and once more, as every editor's find does.
            cursor = self.view.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
            )
            self.view.setTextCursor(cursor)
            found = self.view.find(needle, flags)
            wrapped = found
        if not found:
            self.lbl_find.setText("Not found")
        else:
            self.lbl_find.setText("Wrapped round" if wrapped else "")
        return bool(found)

    def _stored_interval(self, fallback: int) -> int:
        if self._store is None:
            return fallback
        try:
            value = int(self._store.get_pref(self.PREF_INTERVAL, 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return fallback
        # 0 means "never chosen", so the caller's per-backend suggestion stands.
        return min(120, max(1, value)) if value else fallback

    def _stored_enabled(self) -> bool:
        if self._store is None:
            return True
        try:
            return bool(self._store.get_pref(self.PREF_ENABLED, True))
        except AttributeError:
            return True

    def _remember(self) -> None:
        """Keep the auto-refresh choice for the next window.

        Written when the window closes rather than on every step of the spin
        box: the preferences file is rewritten atomically, and holding the up
        arrow should not be a write per second.
        """
        if self._store is None or not hasattr(self, "spin_interval"):
            return
        try:
            self._store.set_pref(self.PREF_INTERVAL, int(self.spin_interval.value()))
            self._store.set_pref(self.PREF_ENABLED, bool(self.chk_auto_refresh.isChecked()))
        except (AttributeError, TypeError, ValueError):
            logging.debug("Job Manager: could not remember the tail interval", exc_info=True)

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

    def set_refresh(self, on_refresh) -> None:
        """Point Refresh, and the auto-refresh timer, at a different source."""
        self._on_refresh_callback = on_refresh

    def set_text(self, text: str) -> None:
        """Replace the contents, keeping the view scrolled to the end.

        The end is where a log is written, so that is what a refresh should
        show without asking the reader to scroll for it every time.
        """
        self.view.setPlainText(text)
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._timer.stop()
        self._remember()
        super().closeEvent(event)

    def reject(self) -> None:
        # Esc and the Close button both come through here without a closeEvent.
        self._timer.stop()
        self._remember()
        super().reject()

    def accept(self) -> None:
        self._timer.stop()
        self._remember()
        super().accept()


__all__ = ["TextDialog"]
