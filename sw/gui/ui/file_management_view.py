"""File Management tab: where recordings land and how they're named. Every recording session
writes two files sharing one prefix and one index -- {prefix}_{index:04d}_fci_live.csv and
{prefix}_{index:04d}_scope_traces.csv (see csv_logger.py) -- so the pair stays associated
afterward. The index is never optional: a bare {prefix}.csv would silently let one recording
overwrite another with no way to tell them apart later.

AppController owns all the naming/overwrite logic; this widget is just the controls.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class FileManagementView(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("CSV Output Directory:"))
        self.txt_csv_dir = QLineEdit()
        self.txt_csv_dir.setReadOnly(True)
        self.txt_csv_dir.setMinimumWidth(420)
        dir_row.addWidget(self.txt_csv_dir)
        self.btn_browse_dir = QPushButton("Browse...")
        dir_row.addWidget(self.btn_browse_dir)
        dir_row.addStretch(1)
        layout.addLayout(dir_row)

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

        layout.addStretch(1)
