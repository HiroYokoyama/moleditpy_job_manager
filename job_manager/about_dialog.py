"""What this plugin is, which version is running, and where its parts live.

Small on purpose. The one question it exists to answer is "which version am I
looking at", because that is what a bug report needs and what the Plugin
Installer's listing does not tell you once the plugin is running.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import (
    PLUGIN_AUTHOR,
    PLUGIN_NAME,
    PLUGIN_OPTIONAL_DEPENDENCIES,
    PLUGIN_SUPPORTED_MOLEDITPY_VERSION,
    PLUGIN_VERSION,
)
from .icon import plugin_icon
from .window_utils import make_independent

REPO_URL = "https://github.com/HiroYokoyama/moleditpy_job_manager"

#: The one-line summary, not PLUGIN_DESCRIPTION. That one is written for the
#: Installer's catalogue and runs to a paragraph; a dialog someone opened to
#: read a version number should not make them scroll past it.
SUMMARY = (
    "Submit calculations to a remote machine over SSH — or to this one, with "
    "no SSH at all — track them, and fetch the results back."
)


class AboutDialog(QDialog):
    """Reached from Extensions > Job Manager > About."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {PLUGIN_NAME}")
        make_independent(self)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)

        heading = QHBoxLayout()
        icon = QLabel()
        # 64, and from the QIcon rather than a fresh render: it is the same
        # drawing the tab and the task bar use, at a size that was baked
        # rather than scaled.
        icon.setPixmap(plugin_icon().pixmap(64, 64))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading.addWidget(icon)

        titles = QVBoxLayout()
        name = QLabel(f"<b style='font-size:15pt'>{PLUGIN_NAME}</b>")
        titles.addWidget(name)
        self.lbl_version = QLabel(f"Version {PLUGIN_VERSION}")
        titles.addWidget(self.lbl_version)
        titles.addWidget(QLabel(f"by {PLUGIN_AUTHOR}"))
        titles.addStretch(1)
        heading.addLayout(titles, 1)
        layout.addLayout(heading)

        summary = QLabel(SUMMARY)
        summary.setWordWrap(True)
        layout.addWidget(summary)

        link = QLabel(f'<a href="{REPO_URL}">{REPO_URL}</a>')
        link.setOpenExternalLinks(True)
        # Selectable as well as clickable: a machine with no browser configured
        # opens nothing at all, and then the text is the only way to get it.
        link.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(link)

        details = QLabel(
            f"For MoleditPy {PLUGIN_SUPPORTED_MOLEDITPY_VERSION}<br>"
            f"Optional: {', '.join(PLUGIN_OPTIONAL_DEPENDENCIES) or 'none'}"
        )
        details.setWordWrap(True)
        layout.addWidget(details)

        buttons = QHBoxLayout()
        self.btn_copy = QPushButton("Copy version")
        self.btn_copy.setToolTip("Copy the version line, for a bug report.")
        self.btn_copy.clicked.connect(self._copy)
        buttons.addWidget(self.btn_copy)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def version_line(self) -> str:
        """What Copy puts on the clipboard, and what a report should quote."""
        return f"{PLUGIN_NAME} {PLUGIN_VERSION}"

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.version_line())
        self.btn_copy.setText("Copied")


__all__ = ["REPO_URL", "SUMMARY", "AboutDialog"]
