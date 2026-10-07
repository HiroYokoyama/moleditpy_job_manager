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
    QAction,
    QCloseEvent,
    QFontDatabase,
    QGuiApplication,
    QKeySequence,
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
    QMenuBar,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .theme import apply_theme
from .window_utils import bring_to_front, make_independent


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
    """Read-only monospaced text, with an optional Reload and Auto-refresh timer."""

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
        follow: bool = True,
        auto_refresh: bool = True,
    ) -> None:
        super().__init__(parent)
        #: When given, the auto-refresh choice is remembered in it. Optional so
        #: this stays a plain text window for callers that have no store.
        self._store = store
        self.setWindowTitle(title)
        make_independent(self)
        apply_theme(self)
        self.resize(820, 560)
        self._on_refresh_callback = on_refresh
        #: Set while set_text replaces the contents, so the scroll bar moving
        #: under it is not taken for the reader scrolling away from the end.
        self._replacing = False

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

        if on_refresh is not None and auto_refresh:
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

        self.chk_follow = QCheckBox("Follow end")
        self.chk_follow.setToolTip(
            "Keep the end of the text in view when it is reloaded.\n"
            "Scrolling up turns this off; scrolling back to the end turns it on."
        )
        self.chk_follow.setChecked(follow)
        self.chk_follow.toggled.connect(self._on_follow_toggled)
        bottom_row.addWidget(self.chk_follow)
        bottom_row.addStretch(1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if on_refresh is not None:
            self.btn_refresh = QPushButton("Reload")
            self.btn_refresh.setToolTip("Read the file again (F5)")
            self.btn_refresh.setAutoDefault(False)
            self.btn_refresh.clicked.connect(self._trigger_refresh)
            box.addButton(self.btn_refresh, QDialogButtonBox.ButtonRole.ActionRole)
        # Close has RejectRole, so the box emits rejected for it. Connecting
        # its clicked as well would call reject() twice and emit finished twice.
        box.rejected.connect(self.reject)
        bottom_row.addWidget(box)
        layout.addLayout(bottom_row)

        layout.setMenuBar(self._build_menu_bar())
        self.view.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    # --- menus -----------------------------------------------------------------

    def _build_menu_bar(self) -> QMenuBar:
        """File / Edit / View, so nothing here is reachable only through a key
        the reader has to know about already -- find used to be Ctrl+F alone."""
        bar = QMenuBar(self)

        file_menu = bar.addMenu("&File")
        self.act_reload = self._action(file_menu, "&Reload", self._trigger_refresh, "F5")
        self.act_reload.setEnabled(self._on_refresh_callback is not None)
        file_menu.addSeparator()
        self._action(file_menu, "&Close", self.reject)

        edit_menu = bar.addMenu("&Edit")
        self._action(edit_menu, "&Copy", self.view.copy)
        self._action(edit_menu, "Copy &All", self.copy_all)
        self._action(edit_menu, "Select A&ll", self.view.selectAll)
        edit_menu.addSeparator()
        self.act_find = self._action(
            edit_menu, "&Find...", self.show_find, QKeySequence.StandardKey.Find
        )
        self._action(edit_menu, "Find &Next", lambda: self.find(False), "F3")
        self._action(edit_menu, "Find &Previous", lambda: self.find(True), "Shift+F3")

        view_menu = bar.addMenu("&View")
        self._action(view_menu, "Go to &Top", self.go_to_top, "Ctrl+Home")
        self._action(view_menu, "Go to &End", self.go_to_end, "Ctrl+End")
        view_menu.addSeparator()
        self.act_follow = self._action(view_menu, "&Follow End", None)
        self.act_follow.setCheckable(True)
        self.act_follow.setChecked(self.chk_follow.isChecked())
        self.act_follow.toggled.connect(self.chk_follow.setChecked)
        self.chk_follow.toggled.connect(self.act_follow.setChecked)
        self.act_wrap = self._action(view_menu, "&Wrap Lines", None)
        self.act_wrap.setCheckable(True)
        self.act_wrap.setChecked(True)
        self.act_wrap.toggled.connect(self.set_wrap)
        return bar

    def _action(self, menu, text: str, slot, shortcut=None) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        if slot is not None:
            action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
        return action

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.view.toPlainText())

    def set_wrap(self, wrap: bool) -> None:
        self.view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        )

    def go_to_top(self) -> None:
        self.view.moveCursor(QTextCursor.MoveOperation.Start)
        self.view.verticalScrollBar().setValue(0)

    def go_to_end(self) -> None:
        self.view.moveCursor(QTextCursor.MoveOperation.End)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_follow_toggled(self, checked: bool) -> None:
        if checked:
            self.go_to_end()

    def _on_scrolled(self, value: int) -> None:
        if self._replacing:
            return
        at_end = value >= self.view.verticalScrollBar().maximum()
        if self.chk_follow.isChecked() != at_end:
            self.chk_follow.setChecked(at_end)

    def present(self) -> None:
        """Show the window in front, and keep it there.

        Opened from a modal chooser (Open Results, Tail Specific File), the
        viewer came up first and the chooser closed after it, which hands
        activation back to the chooser's owner -- the monitor -- and buried the
        new window behind it. Raising once more after the event loop has
        finished closing the chooser leaves it on top.
        """
        bring_to_front(self)
        QTimer.singleShot(0, self._raise_if_open)

    def _raise_if_open(self) -> None:
        try:
            if self.isVisible():
                bring_to_front(self)
        except RuntimeError:
            pass

    # --- find ------------------------------------------------------------------

    def _build_find_bar(self) -> QWidget:
        """Edit > Find or Ctrl+F opens it; Enter / F3 finds the next match,
        with Shift the one before; Esc or its close button closes it. Hidden
        until asked for."""
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
        self.lbl_find.setMinimumWidth(110)
        row.addWidget(self.lbl_find)
        self.btn_close_find = QToolButton()
        self.btn_close_find.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        self.btn_close_find.setToolTip("Close the find bar (Esc)")
        self.btn_close_find.setAutoRaise(True)
        self.btn_close_find.clicked.connect(self.hide_find)
        row.addWidget(self.btn_close_find)
        self.find_bar.setVisible(False)
        return self.find_bar

    def show_find(self) -> None:
        # Whatever is selected in the text is what the user most likely wants.
        selected = self.view.textCursor().selectedText()
        if selected and " " not in selected:
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
            where = self._match_position(needle)
            self.lbl_find.setText(f"{where}, wrapped round" if wrapped else where)
        return bool(found)

    def _match_position(self, needle: str) -> str:
        """'3 of 12' for the selected match."""
        text = self.view.toPlainText()
        if not self.chk_case.isChecked():
            text, needle = text.lower(), needle.lower()
        total = text.count(needle)
        before = text.count(needle, 0, self.view.textCursor().selectionStart())
        return f"{before + 1} of {total}"

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Esc closes the find bar first and the window only after: with the
        # focus back in the text, a second Esc used to be the first one.
        if event.key() == Qt.Key.Key_Escape and self.find_bar.isVisible():
            self.hide_find()
            event.accept()
            return
        super().keyPressEvent(event)

    # --- auto-refresh ----------------------------------------------------------

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
        """Point Reload, and the auto-refresh timer, at a different source."""
        self._on_refresh_callback = on_refresh
        self.act_reload.setEnabled(on_refresh is not None)

    def set_text(self, text: str) -> None:
        """Replace the contents: at the end while following it, otherwise
        where the reader was.

        Every refresh used to jump to the end, so with auto-refresh on nothing
        further up a log could be read -- a few seconds later the place was
        gone. The end is still where a log is written, so following it stays
        the default until the reader scrolls away.
        """
        bar = self.view.verticalScrollBar()
        hbar = self.view.horizontalScrollBar()
        old_value, old_h = bar.value(), hbar.value()
        old_cursor = self.view.textCursor()
        anchor, position = old_cursor.anchor(), old_cursor.position()
        self._replacing = True
        try:
            self.view.setPlainText(text)
            # Kept so a selected match, and the next Find from it, survive.
            end = len(self.view.toPlainText())
            cursor = self.view.textCursor()
            cursor.setPosition(min(anchor, end))
            cursor.setPosition(min(position, end), QTextCursor.MoveMode.KeepAnchor)
            self.view.setTextCursor(cursor)
            if self.chk_follow.isChecked():
                bar.setValue(bar.maximum())
            else:
                bar.setValue(min(old_value, bar.maximum()))
            hbar.setValue(old_h)
        finally:
            self._replacing = False

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
