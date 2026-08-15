"""Editor for the two kinds of command MoleditPy remembers for you.

Per-extension defaults (``store.default_command_for``) and named templates
(``store.user_templates()``) could previously only be *added*, from the submit
wizard's dropdown -- a saved template could be deleted one at a time through a
QInputDialog picker, but a per-extension default had no delete path in the UI
at all short of editing settings.json by hand. This dialog lists both, and
lets either be edited or removed.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import PLUGIN_VERSION
from .theme import apply_theme
from .window_utils import make_independent

#: Shown wherever a command line is edited here, so the placeholders are
#: explained at the point someone is most likely to type one.
PLACEHOLDER_TIP = (
    "Placeholders substituted at submit time:\n"
    "  {input}     the uploaded file's name, e.g. water.inp\n"
    "  {stem}      the same, without its extension, e.g. water\n"
    "  {basename}  same as {stem} (kept for older templates)\n"
    "  {name}      the job's display name\n"
    "  {jobdir}    the job's directory on the host\n"
    "  {nodes} {ntasks} {cpus} {memory} {queue} {walltime}\n"
    "              the preset's resource request"
)

EXT_ROLE = Qt.ItemDataRole.UserRole
CMD_ROLE = Qt.ItemDataRole.UserRole + 1
GLOBS_ROLE = Qt.ItemDataRole.UserRole + 2


class TemplateEditorDialog(QDialog):
    """Manage per-extension default commands and named command templates."""

    def __init__(self, store, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle(f"Job Manager {PLUGIN_VERSION} - Command Templates")
        make_independent(self)
        apply_theme(self)
        self.resize(620, 520)
        self._build_ui()
        self._reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Commands you have told the submit wizard to remember: one per "
            "input extension ('use this for every .inp'), and any you saved "
            "under a name of your own."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)

        defaults_box = QGroupBox("Default command per extension")
        defaults_layout = QVBoxLayout(defaults_box)
        self.list_defaults = QListWidget()
        self.list_defaults.setToolTip(PLACEHOLDER_TIP)
        self.list_defaults.itemDoubleClicked.connect(self._edit_default)
        defaults_layout.addWidget(self.list_defaults)
        defaults_buttons = QHBoxLayout()
        btn_edit_default = QPushButton("Edit...")
        btn_edit_default.clicked.connect(self._edit_selected_default)
        btn_delete_default = QPushButton("Delete")
        btn_delete_default.clicked.connect(self._delete_selected_default)
        defaults_buttons.addWidget(btn_edit_default)
        defaults_buttons.addWidget(btn_delete_default)
        defaults_buttons.addStretch(1)
        defaults_layout.addLayout(defaults_buttons)
        layout.addWidget(defaults_box, 1)

        templates_box = QGroupBox("Saved templates")
        templates_layout = QVBoxLayout(templates_box)
        self.list_templates = QListWidget()
        self.list_templates.setToolTip(PLACEHOLDER_TIP)
        self.list_templates.itemDoubleClicked.connect(self._edit_template)
        templates_layout.addWidget(self.list_templates)
        templates_buttons = QHBoxLayout()
        btn_edit_template = QPushButton("Edit...")
        btn_edit_template.clicked.connect(self._edit_selected_template)
        btn_delete_template = QPushButton("Delete")
        btn_delete_template.clicked.connect(self._delete_selected_template)
        templates_buttons.addWidget(btn_edit_template)
        templates_buttons.addWidget(btn_delete_template)
        templates_buttons.addStretch(1)
        templates_layout.addLayout(templates_buttons)
        layout.addWidget(templates_box, 1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(box)

    # --- loading --------------------------------------------------------

    def _reload(self) -> None:
        self.list_defaults.clear()
        defaults = self.store.get_pref("default_commands", {}) or {}
        for extension in sorted(defaults):
            entry = defaults.get(extension) or {}
            command = str(entry.get("command", ""))
            globs = list(entry.get("fetch_globs") or [])
            item = QListWidgetItem(f"{extension}  →  {command}")
            item.setData(EXT_ROLE, extension)
            item.setData(CMD_ROLE, command)
            item.setData(GLOBS_ROLE, globs)
            item.setToolTip(command)
            self.list_defaults.addItem(item)
        if not defaults:
            placeholder = QListWidgetItem("No per-extension defaults yet.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_defaults.addItem(placeholder)

        self.list_templates.clear()
        templates = self.store.user_templates()
        for entry in templates:
            label = entry.get("label", "")
            command = entry.get("command", "")
            item = QListWidgetItem(f"{label}  →  {command}")
            item.setData(EXT_ROLE, label)
            item.setData(CMD_ROLE, command)
            item.setData(GLOBS_ROLE, list(entry.get("fetch_globs") or []))
            item.setToolTip(command)
            self.list_templates.addItem(item)
        if not templates:
            placeholder = QListWidgetItem("No saved templates yet.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_templates.addItem(placeholder)

    # --- default commands -------------------------------------------------

    def _edit_selected_default(self) -> None:
        item = self.list_defaults.currentItem()
        if item is not None:
            self._edit_default(item)

    def _edit_default(self, item: QListWidgetItem) -> None:
        extension = item.data(EXT_ROLE)
        if not extension:
            return
        command, accepted = QInputDialog.getText(
            self,
            f"Default command for {extension}",
            f"Command run for every {extension} input:\n\n{PLACEHOLDER_TIP}",
            QLineEdit.EchoMode.Normal,
            item.data(CMD_ROLE) or "",
        )
        if not accepted:
            return
        self.store.set_default_command(extension, command.strip(), item.data(GLOBS_ROLE) or [])
        self._reload()

    def _delete_selected_default(self) -> None:
        item = self.list_defaults.currentItem()
        extension = item.data(EXT_ROLE) if item is not None else None
        if not extension:
            return
        confirm = QMessageBox.question(
            self, "Delete default", f"Stop remembering a default command for {extension}?"
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # An empty command is what set_default_command treats as "forget it".
        self.store.set_default_command(extension, "", [])
        self._reload()

    # --- named templates ----------------------------------------------------

    def _edit_selected_template(self) -> None:
        item = self.list_templates.currentItem()
        if item is not None:
            self._edit_template(item)

    def _edit_template(self, item: QListWidgetItem) -> None:
        label = item.data(EXT_ROLE)
        if not label:
            return
        command, accepted = QInputDialog.getText(
            self,
            f"Template: {label}",
            f"Command for '{label}':\n\n{PLACEHOLDER_TIP}",
            QLineEdit.EchoMode.Normal,
            item.data(CMD_ROLE) or "",
        )
        if not accepted:
            return
        self.store.add_user_template(label, command.strip(), item.data(GLOBS_ROLE) or [])
        self._reload()

    def _delete_selected_template(self) -> None:
        item = self.list_templates.currentItem()
        label = item.data(EXT_ROLE) if item is not None else None
        if not label:
            return
        confirm = QMessageBox.question(self, "Delete template", f"Delete the template '{label}'?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_user_template(label)
        self._reload()


__all__ = ["TemplateEditorDialog", "PLACEHOLDER_TIP"]
