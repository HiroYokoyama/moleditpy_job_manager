"""Everything recorded about one job, with the few parts still worth changing.

Most of a job record is history: the host it went to, the script that ran, the
exit code it came back with. Those are shown and not touched -- editing them
would describe a job that never existed.

Four things are not history, because they decide what happens *next*: the name,
whether results are fetched when it ends, which files count as results, and
where they land. All four are routinely wrong exactly once -- after a job has
been submitted and before it has finished -- which is the one moment the old
read-only view offered nothing but retyping the submission.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtGui import QFontDatabase

from .window_utils import make_independent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .models import Job


class JobDetailsDialog(QDialog):
    """The job record, plus the settings that still apply to its results."""

    def __init__(
        self,
        service,
        job: Job,
        record: str,
        title: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.job = job
        self.setWindowTitle(title)
        make_independent(self)
        self.resize(820, 620)

        layout = QVBoxLayout(self)

        box = QGroupBox("Settings that still apply")
        form = QFormLayout(box)
        self.txt_name = QLineEdit(job.name)
        self.txt_name.setToolTip("What this job is called in the table and in notifications.")
        self.txt_globs = QLineEdit(", ".join(job.fetch_globs or []))
        self.txt_globs.setToolTip(
            "Which files are fetched from the job directory when it ends.\n\n"
            "Comma separated, e.g. *.out, *.log, *.xyz. Wrong patterns are the "
            "usual reason a finished job downloads nothing -- and they can be "
            "corrected here while it is still running."
        )
        self.chk_auto = QCheckBox("Download when the job ends")
        self.chk_auto.setChecked(bool(job.auto_download))
        self.chk_auto.setToolTip(
            "Fetch the results as soon as the job finishes. Unticked, they stay "
            "on the host until you press Download."
        )

        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.txt_local = QLineEdit(job.local_dir)
        self.txt_local.setPlaceholderText("decided when downloading (beside the input)")
        self.txt_local.setToolTip(
            "Where the results are written.\n\n"
            "Left empty, it is chosen when the download happens: beside the "
            "input file, or the shared download folder for a job that has no "
            "local input to sit beside."
        )
        browse = QPushButton("...")
        browse.setMaximumWidth(32)
        browse.clicked.connect(self._browse)
        folder_layout.addWidget(self.txt_local)
        folder_layout.addWidget(browse)

        form.addRow("Name", self.txt_name)
        form.addRow("Fetch patterns", self.txt_globs)
        form.addRow("Results folder", folder_row)
        form.addRow("", self.chk_auto)
        layout.addWidget(box)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setPlainText(record)
        layout.addWidget(self.view, 1)

        self.lbl_saved = QLabel("")
        self.lbl_saved.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.lbl_saved)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self._save)
        # Only through rejected: a Close button already emits it, and wiring
        # its clicked as well called reject() twice, so finished fired twice.
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Results folder", self.txt_local.text())
        if path:
            self.txt_local.setText(path)

    def _save(self) -> None:
        """Apply the four fields and persist, then let the table catch up."""
        self.job.name = self.txt_name.text().strip() or self.job.name
        self.job.fetch_globs = [
            pattern.strip() for pattern in self.txt_globs.text().split(",") if pattern.strip()
        ]
        self.job.local_dir = self.txt_local.text().strip()
        self.job.auto_download = bool(self.chk_auto.isChecked())
        self.service.store.save_jobs()
        self.service.jobs_changed.emit()
        # Said plainly rather than in the title bar, which is where the job's
        # own name lives and should stay.
        self.lbl_saved.setText("Saved. The patterns and folder apply to the next download.")


__all__ = ["JobDetailsDialog"]
