"""New Project dialog: name, location, and an optional description, with a live preview of the
folder that will be created.

One dialog rather than the two a stock Qt flow would need (a name prompt, then a folder picker):
what is being created is a DIRECTORY, and QFileDialog's save-file mode -- the only single-dialog
stock option -- presents that as saving a file, which misdescribes the project layout badly enough
to be worth a purpose-built form. The preview line exists for the same reason: the user is choosing
a parent directory and a name, and what they actually get is the join of the two.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

INVALID_NAME_CHARS = re.compile(r"[\\/:*?\"<>|]")
"""Rejected outright rather than silently substituted. A project's folder name is also how the user
identifies it afterward, so quietly turning "Cs-137 4/9" into "Cs-137 4_9" would rename their
campaign behind their back; the two the POSIX filesystem genuinely cannot take (/ and NUL) are
joined here by the Windows-reserved set so a project folder stays portable to a machine that has to
read the same dataset."""


class NewProjectDialog(QDialog):
    def __init__(self, default_parent_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "A project is a folder holding this campaign's settings and all of its recorded data "
            "(RAW/, LIST/, SPECTRA/)."
        ))

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Project name:"))
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("e.g. clyc-DD-20260907")
        self.txt_name.textChanged.connect(self._update_preview)
        name_row.addWidget(self.txt_name)
        layout.addLayout(name_row)

        loc_row = QHBoxLayout()
        loc_row.addWidget(QLabel("Location:"))
        self.txt_location = QLineEdit(str(default_parent_dir))
        self.txt_location.textChanged.connect(self._update_preview)
        loc_row.addWidget(self.txt_location)
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._browse)
        loc_row.addWidget(self.btn_browse)
        layout.addLayout(loc_row)

        layout.addWidget(QLabel("Description (optional):"))
        self.txt_description = QPlainTextEdit()
        self.txt_description.setMaximumHeight(70)
        layout.addWidget(self.txt_description)

        self.lbl_preview = QLabel("")
        self.lbl_preview.setWordWrap(True)
        layout.addWidget(self.lbl_preview)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._update_preview()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select Project Location", self.txt_location.text()
        )
        if chosen:
            self.txt_location.setText(chosen)

    def _update_preview(self) -> None:
        """Also the validator: OK stays disabled, with the reason shown, until the inputs describe
        a directory that can actually be created. Checking at accept() time instead would put the
        same message in a second dialog after the user has already committed."""
        name = self.txt_name.text().strip()
        location = self.txt_location.text().strip()
        problem = ""
        if not name:
            problem = "Enter a project name."
        elif INVALID_NAME_CHARS.search(name):
            problem = "The project name cannot contain \\ / : * ? \" < > |."
        elif not location:
            problem = "Choose a location."
        elif (Path(location) / name).exists():
            problem = f"{Path(location) / name} already exists."

        if problem:
            self.lbl_preview.setText(f"⚠ {problem}")
            self.lbl_preview.setStyleSheet("color: #c0392b;")
        else:
            self.lbl_preview.setText(f"Will create: {Path(location) / name}")
            self.lbl_preview.setStyleSheet("color: #555555;")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not problem)

    # ------------------------------------------------------------------------------------ result

    def project_name(self) -> str:
        return self.txt_name.text().strip()

    def project_location(self) -> Path:
        return Path(self.txt_location.text().strip())

    def project_description(self) -> str:
        return self.txt_description.toPlainText().strip()
