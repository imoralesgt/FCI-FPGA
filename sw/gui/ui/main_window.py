"""Top-level window shell: connection row, CSV directory row, and a tab widget hosting the three
views. Mirrors the reference GUI's overall structure (connection bar, then tabs) with the
tab-per-domain split this project's richer state (live acquisition / scope / 6 config subsystems)
calls for, instead of one tab per counter channel.
"""

import logging

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ui.config_panel import ConfigPanel
from ui.live_view import LiveView
from ui.scope_view import ScopeView

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        logger.info("Constructing main window.")
        self.setWindowTitle("FCI-FPGA Client")
        self.resize(1200, 760)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        conn_layout = QHBoxLayout()
        conn_layout.addWidget(QLabel("Serial Port:"))
        self.combo_ports = QComboBox()
        self.combo_ports.setMinimumWidth(280)
        conn_layout.addWidget(self.combo_ports)
        self.btn_refresh_ports = QPushButton("Refresh")
        conn_layout.addWidget(self.btn_refresh_ports)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setStyleSheet("font-weight: bold;")
        conn_layout.addWidget(self.btn_connect)
        conn_layout.addStretch(1)
        self.lbl_status = QLabel("\U0001F534 Disconnected")
        conn_layout.addWidget(self.lbl_status)
        main_layout.addLayout(conn_layout)

        dir_layout = QHBoxLayout()
        dir_layout.addWidget(QLabel("CSV Output Directory:"))
        self.txt_csv_dir = QLineEdit()
        self.txt_csv_dir.setReadOnly(True)
        self.txt_csv_dir.setMinimumWidth(420)
        dir_layout.addWidget(self.txt_csv_dir)
        self.btn_browse_dir = QPushButton("Browse...")
        dir_layout.addWidget(self.btn_browse_dir)
        dir_layout.addStretch(1)
        main_layout.addLayout(dir_layout)

        self.tabs = QTabWidget()
        self.live_view = LiveView()
        self.scope_view = ScopeView()
        self.config_panel = ConfigPanel()
        self.tabs.addTab(self.live_view, "Live FCI/PSD")
        self.tabs.addTab(self.scope_view, "Oscilloscope")
        self.tabs.addTab(self.config_panel, "Configuration")
        main_layout.addWidget(self.tabs)

        self.set_connected_controls_enabled(False)

    def set_connected_controls_enabled(self, enabled: bool) -> None:
        self.live_view.set_controls_enabled(enabled)
        self.scope_view.set_controls_enabled(enabled)
        self.config_panel.set_controls_enabled(enabled)
