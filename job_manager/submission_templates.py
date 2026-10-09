"""Command-template selection and preferences, independent of submission."""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QInputDialog, QMessageBox

from .command_templates import CommandTemplate, extension_of, suggest, templates_for
from .submission_config import _DELETE_TEMPLATE, _MANAGE_TEMPLATES, _SAVE_TEMPLATE, _SET_DEFAULT


class SubmissionTemplates:
    def __init__(self, store, selected_files, command, globs, combo, parent):
        self.store = store
        self.selected_files = selected_files
        self.txt_command = command
        self.txt_globs = globs
        self.cmb_template = combo
        self.parent = parent
        self._globs_before_template = []

    def _reload_templates(self) -> None:
        """Refill the dropdown, most likely program first for these inputs."""
        files = self.selected_files()
        self.cmb_template.blockSignals(True)
        self.cmb_template.clear()
        self.cmb_template.addItem("Template...", None)

        for template in templates_for(os.path.basename(files[0]) if files else ""):
            self.cmb_template.addItem(template.label, template)
            if template.note:
                self.cmb_template.setItemData(
                    self.cmb_template.count() - 1,
                    f"{template.command or '(type your own)'}\n\n{template.note}",
                    Qt.ItemDataRole.ToolTipRole,
                )

        saved = self.store.user_templates()
        if saved:
            self.cmb_template.insertSeparator(self.cmb_template.count())
            for entry in saved:
                self.cmb_template.addItem(
                    entry["label"],
                    CommandTemplate(
                        entry["label"],
                        entry["command"],
                        fetch_globs=tuple(entry.get("fetch_globs") or ()),
                    ),
                )
                self.cmb_template.setItemData(
                    self.cmb_template.count() - 1,
                    entry["command"],
                    Qt.ItemDataRole.ToolTipRole,
                )

        self.cmb_template.insertSeparator(self.cmb_template.count())
        extension = extension_of(os.path.basename(files[0])) if files else ""
        if extension:
            # ORCA, CP2K and GAMESS all write .inp, so the wizard will not
            # guess -- but it will remember which one this user means.
            self.cmb_template.addItem(f"Use this command for every {extension}", _SET_DEFAULT)
        self.cmb_template.addItem("Save current command as...", _SAVE_TEMPLATE)
        if saved:
            self.cmb_template.addItem("Delete a saved template...", _DELETE_TEMPLATE)
        self.cmb_template.addItem("Manage templates...", _MANAGE_TEMPLATES)
        self.cmb_template.blockSignals(False)

    def _on_template_chosen(self, index: int) -> None:
        choice = self.cmb_template.itemData(index)
        self.cmb_template.setCurrentIndex(0)
        if choice is _SAVE_TEMPLATE:
            self._save_user_template()
        elif choice is _SET_DEFAULT:
            self._set_default_for_extension()
        elif choice is _DELETE_TEMPLATE:
            self._delete_user_template()
        elif choice is _MANAGE_TEMPLATES:
            self._manage_templates()
        elif choice is not None:
            self.txt_command.setText(choice.command)
            self._apply_template_globs(choice)

    def _apply_template_globs(self, template: CommandTemplate) -> None:
        """Take the fetch patterns from the program that was just chosen.

        Never over a list the user has edited: a filled field is a decision.
        """
        if not template.fetch_globs:
            return
        current = [g.strip() for g in self.txt_globs.text().split(",") if g.strip()]
        if current and current != self._globs_before_template:
            return
        self.txt_globs.setText(", ".join(template.fetch_globs))
        self._globs_before_template = list(template.fetch_globs)

    def _set_default_for_extension(self) -> None:
        """Make this command what an input of that extension gets from now on."""
        files = self.selected_files()
        extension = extension_of(os.path.basename(files[0])) if files else ""
        command = self.txt_command.text().strip()
        if not extension:
            return
        if not command:
            QMessageBox.information(self.parent, "Default command", "Enter a command first.")
            return
        globs = [g.strip() for g in self.txt_globs.text().split(",") if g.strip()]
        self.store.set_default_command(extension, command, globs)
        QMessageBox.information(
            self,
            "Default command",
            f"Every {extension} added from now on starts with this command"
            + (" and these fetch patterns." if globs else "."),
        )
        self._reload_templates()

    def _save_user_template(self) -> None:
        command = self.txt_command.text().strip()
        if not command:
            QMessageBox.information(self.parent, "Save template", "Enter a command first.")
            return
        label, accepted = QInputDialog.getText(
            self.parent, "Save template", "Name for this command template:"
        )
        if not accepted or not label.strip():
            return
        globs = [g.strip() for g in self.txt_globs.text().split(",") if g.strip()]
        self.store.add_user_template(label.strip(), command, globs)
        self._reload_templates()

    def _delete_user_template(self) -> None:
        labels = [entry["label"] for entry in self.store.user_templates()]
        if not labels:
            return
        label, accepted = QInputDialog.getItem(
            self.parent, "Delete template", "Remove which template?", labels, 0, False
        )
        if accepted and label:
            self.store.remove_user_template(label)
            self._reload_templates()

    def _manage_templates(self) -> None:
        from .template_editor_dialog import TemplateEditorDialog

        TemplateEditorDialog(self.store, self.parent).exec()
        self._reload_templates()

    def _apply_suggested_template(self, force: bool = False) -> None:
        """Fill an empty command from the input's extension; if force=True, overwrites."""
        files = self.selected_files()
        if not files:
            return
        if not force and self.txt_command.text().strip():
            return
        filename = os.path.basename(files[0])
        ext = extension_of(filename)
        # The user's own answer first, ahead of the built-in guess list.
        stored = self.store.default_command_for(ext)
        if stored.get("command"):
            self.txt_command.setText(stored["command"])
            self._apply_template_globs(
                CommandTemplate(
                    "", stored["command"], fetch_globs=tuple(stored.get("fetch_globs") or ())
                )
            )
            return
        template = suggest(filename)
        if template is not None and template.command:
            self.txt_command.setText(template.command)
            self._apply_template_globs(template)
            for i in range(self.cmb_template.count()):
                if self.cmb_template.itemText(i) == template.label:
                    self.cmb_template.setCurrentIndex(i)
                    break
