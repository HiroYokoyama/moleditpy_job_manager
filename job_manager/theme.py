"""Design tokens and shared stylesheet for the Job Manager UI.

Every colour and the shared stylesheet live here so all dialogs agree and
edits happen in one place.  The stylesheet uses ``palette()`` roles wherever
possible -- it adapts to light *and* dark mode without branching -- and only
reaches for a fixed hex when a specific accent is wanted.

Dark-mode note: ``setStyleSheet`` on a widget that already has one will *not*
re-read the palette after the system switches themes.  Widgets that need a
dynamic accent colour (e.g. the status bar counter) must override
``changeEvent`` and re-apply their colour there instead of locking it in
at construction time.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# State colours -- imported by jobs_dialog and status_widget
# ---------------------------------------------------------------------------

#: Green for RUNNING / busy.
CY_GREEN = "#00e676"
#: Red for FAILED / BLOCKED.
CY_RED = "#ff2d55"
#: Amber for PENDING / warnings.
CY_AMBER = "#ffb700"
#: Teal for DONE.
CY_TEAL = "#14b8a6"
#: Purple for LOST.
CY_PURPLE = "#c77dff"
#: Neutral grey for QUEUED.
CY_GREY = "#8898aa"

# ---------------------------------------------------------------------------
# Accent colours for the UI chrome
# ---------------------------------------------------------------------------

#: Primary blue accent.
CY_ACCENT = "#2979ff"
#: Secondary blue accent.
CY_ACCENT2 = "#1565c0"

# ---------------------------------------------------------------------------
# Shared dialog stylesheet
# ---------------------------------------------------------------------------

# The stylesheet is applied to the top-level dialog widget.  Children inherit
# it unless they override it themselves.  Every ``palette(...)`` reference
# resolves through Qt's current palette, so dark-mode users get dark surfaces
# and light-mode users get light ones.  Fixed hex appears only for the blue
# accent that is the same in both modes, and only on interactive states
# (hover, focus, selection) -- resting widgets use palette() and look natural.

DIALOG_STYLESHEET = f"""
/* --- push buttons --------------------------------------------------------- */

QPushButton {{
    background: palette(button);
    color: palette(button-text);
    border: 1px solid palette(mid);
    border-radius: 5px;
    padding: 4px 14px;
    min-height: 22px;
}}

QPushButton:hover {{
    background: rgba(41, 121, 255, 0.12);
    border-color: {CY_ACCENT};
    color: {CY_ACCENT};
}}

QPushButton:pressed {{
    background: rgba(41, 121, 255, 0.22);
    border-color: {CY_ACCENT};
}}

QPushButton:disabled {{
    color: palette(mid);
    border-color: palette(mid);
}}

QPushButton:checked {{
    background: rgba(41, 121, 255, 0.15);
    border-color: {CY_ACCENT};
    color: {CY_ACCENT};
}}

/* --- table ---------------------------------------------------------------- */

QTableView {{
    gridline-color: transparent;
    border: 1px solid palette(mid);
    border-radius: 6px;
    alternate-background-color: palette(alternate-base);
    selection-background-color: rgba(41, 121, 255, 0.28);
    selection-color: palette(text);
    outline: none;
}}

QTableView::item {{
    padding: 2px 6px;
    border: none;
}}

QHeaderView::section {{
    background: palette(button);
    color: palette(mid);
    border: none;
    border-bottom: 1px solid palette(mid);
    padding: 4px 8px;
}}

/* --- tree ----------------------------------------------------------------- */

QTreeWidget {{
    border: 1px solid palette(mid);
    border-radius: 6px;
    outline: none;
}}

QTreeWidget::item:selected {{
    background: rgba(41, 121, 255, 0.28);
    color: palette(text);
}}

QTreeWidget::item:hover:!selected {{
    background: rgba(41, 121, 255, 0.08);
}}

/* --- text/plain edit ------------------------------------------------------ */

QPlainTextEdit, QTextEdit {{
    background: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 6px;
    padding: 4px;
}}

QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {CY_ACCENT2};
}}

/* --- line edit ------------------------------------------------------------ */

QLineEdit {{
    background: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 3px 6px;
    min-height: 20px;
}}

QLineEdit:focus {{
    border-color: {CY_ACCENT2};
}}

QLineEdit:disabled {{
    color: palette(mid);
}}

/* --- spin box ------------------------------------------------------------- */

QSpinBox {{
    background: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 2px 20px 2px 4px;
    min-height: 22px;
}}

QSpinBox:focus {{
    border-color: {CY_ACCENT2};
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

/* --- combo box ------------------------------------------------------------ */

QComboBox {{
    background: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 3px 6px;
    min-height: 20px;
}}

QComboBox:focus {{
    border-color: {CY_ACCENT2};
}}

QComboBox QAbstractItemView {{
    background: palette(base);
    border: 1px solid {CY_ACCENT2};
    selection-background-color: rgba(41, 121, 255, 0.28);
}}

/* --- group box ------------------------------------------------------------ */

QGroupBox {{
    border: 1px solid palette(mid);
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 6px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: palette(mid);
}}

/* --- list widget ---------------------------------------------------------- */

QListWidget {{
    background: palette(base);
    border: 1px solid palette(mid);
    border-radius: 6px;
    outline: none;
}}

QListWidget::item:selected {{
    background: rgba(41, 121, 255, 0.28);
    color: palette(text);
}}

QListWidget::item:hover:!selected {{
    background: rgba(41, 121, 255, 0.08);
}}

/* --- scroll bars ---------------------------------------------------------- */

QScrollBar:vertical {{
    background: transparent;
    width: 7px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: palette(mid);
    border-radius: 3px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {CY_ACCENT2};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 7px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: palette(mid);
    border-radius: 3px;
    min-width: 24px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {CY_ACCENT2};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* --- dialog button box ---------------------------------------------------- */

QDialogButtonBox QPushButton {{
    min-width: 72px;
}}

/* --- splitter ------------------------------------------------------------- */

QSplitter::handle {{
    background: palette(mid);
}}

QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

/* --- scroll area ---------------------------------------------------------- */

QScrollArea {{
    border: none;
}}
"""


__all__ = [
    "CY_GREEN",
    "CY_RED",
    "CY_AMBER",
    "CY_TEAL",
    "CY_PURPLE",
    "CY_GREY",
    "CY_ACCENT",
    "CY_ACCENT2",
    "DIALOG_STYLESHEET",
]
