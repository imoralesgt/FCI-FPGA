"""File Management tab: how recordings are named within the open project. Every recording session
writes two files sharing one prefix and one index -- {prefix}_{index:04d}_fci_live.csv into the
project's LIST/ and {prefix}_{index:04d}_scope_traces.csv into its RAW/ (see csv_logger.py) -- so the
pair stays associated afterward. The index is never optional: a bare {prefix}.csv would silently let
one recording overwrite another with no way to tell them apart later.

There is deliberately no output-directory control here any more: a project OWNS where its data goes
(project.py's LIST_DIRNAME/RAW_DIRNAME), the same way CoMPASS ties a project's raw/list output to
the project folder rather than to a directory chosen freely per session. Recording without a project
open is not a supported state at all -- MainWindow disables every hardware control, this tab
included, until Project > New or Open has been used (see main_window.py's set_project_open()) -- so
there is no "no project" branch to design a directory picker for.

AppController owns all the naming/overwrite logic and tells this widget where the project's
directories are; this widget is just the controls and the read-only display of that answer.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


class FileManagementView(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel("Filename prefix:"))
        self.txt_file_prefix = QLineEdit()
        self.txt_file_prefix.setMinimumWidth(200)
        self.txt_file_prefix.setToolTip(
            "The _fci_live/_scope_traces suffix and the _NNNN index are fixed -- only this part "
            "is yours to set."
        )
        prefix_row.addWidget(self.txt_file_prefix)
        self.chk_autoincrement = QCheckBox("Autoincrement index")
        self.chk_autoincrement.setChecked(True)
        self.chk_autoincrement.setToolTip(
            "On: each recording gets the next free index, so nothing is ever overwritten. Off: "
            "always index 0001 -- recording again with the same prefix will overwrite the "
            "previous files (you'll be warned first)."
        )
        prefix_row.addWidget(self.chk_autoincrement)
        prefix_row.addStretch(1)
        layout.addLayout(prefix_row)

        self.lbl_filename_preview = QLabel("")
        self.lbl_filename_preview.setWordWrap(True)
        layout.addWidget(self.lbl_filename_preview)

        self.lbl_output_note = QLabel("")
        self.lbl_output_note.setWordWrap(True)
        self.lbl_output_note.setStyleSheet("color: #555555;")
        layout.addWidget(self.lbl_output_note)
        self.set_project_dirs(None, None)

        layout.addStretch(1)

    def set_project_dirs(self, list_dir: Path | None, raw_dir: Path | None) -> None:
        """Updates the read-only display of where recordings will go. None/None only while no
        project is open -- reachable transiently during startup and shutdown, since the whole tab is
        also disabled at those times (see main_window.py's set_project_open())."""
        if list_dir is None:
            self.lbl_output_note.setText(
                "No project open -- use Project > New or Open to set where recordings are written."
            )
            return
        self.lbl_output_note.setText(
            f"Event CSVs -> {list_dir}\n"
            f"Scope traces -> {raw_dir}"
        )
