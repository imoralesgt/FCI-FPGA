"""Top-level window shell: connection row, CSV directory row, and a tab widget hosting the three
views. Mirrors the reference GUI's overall structure (connection bar, then tabs) with the
tab-per-domain split this project's richer state (live acquisition / scope / 6 config subsystems)
calls for, instead of one tab per counter channel.
"""

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
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
        # Tall enough that the Live FCI/PSD tab's content (two rows of config+plot+stats) fits
        # inside its scroll area without a scrollbar at this default size -- see _scrollable()
        # below for why a scrollbar exists at all (only smaller manual resizes should trigger it).
        self.resize(1200, 920)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        main_layout = QVBoxLayout(self.central_widget)

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

        # The dot is its own QLabel, not text baked into the checkbox: an emoji/dingbat glyph like
        # "⬤" often renders from a color-emoji font that ignores the checkbox's own text color, so
        # styling it through the checkbox's stylesheet alone left it black instead of red.
        self.lbl_record_icon = QLabel("●")
        self.lbl_record_icon.setStyleSheet("color: #e63030; font-size: 13px;")
        dir_layout.addWidget(self.lbl_record_icon)
        self.chk_record = QCheckBox("Record")
        self.chk_record.setStyleSheet("QCheckBox { font-weight: bold; padding: 2px 8px; }")
        self.chk_record.setChecked(True)  # armed by default -- see AppController's Start confirm
        dir_layout.addWidget(self.chk_record)

        dir_layout.addStretch(1)
        main_layout.addLayout(dir_layout)

        self.tabs = QTabWidget()
        self.live_view = LiveView()
        self.scope_view = ScopeView()
        self.config_panel = ConfigPanel()
        # Each tab's content now includes full config forms (FCI/PSD embedded in the live view,
        # Trigger embedded in the scope view) on top of the plots -- taller than fits on many
        # screens at once. Wrapping each tab in its own scroll area (config_panel already did this
        # internally) means that content scrolls instead of forcing the whole window to grow past
        # the screen to satisfy the layout's combined minimum size.
        self.tabs.addTab(self._scrollable(self.live_view), "Live FCI/PSD")
        self.tabs.addTab(self._scrollable(self.scope_view), "Oscilloscope")
        self.tabs.addTab(self.config_panel, "Configuration")
        main_layout.addWidget(self.tabs)

        # PSD's Pre-trigger and the oscilloscope Trigger's Delay are the same physical quantity as
        # far as firmware is concerned (PSD_FIELDS' own tooltip says as much: psd_core needs to
        # know where trigger_core's delay put the pulse in the capture window) -- keep the two
        # controls' pending values mirrored live as either is edited, so committing either panel's
        # Apply independently can never leave them mismatched. This only syncs what's displayed in
        # the not-yet-applied controls; it does not write to the device by itself.
        psd_pre_trigger = self.live_view.psd_config._controls["pre_trigger"]
        trig_delay = self.scope_view.trigger_config._controls["delay"]
        psd_pre_trigger.valueChanged.connect(trig_delay.setValue)
        trig_delay.valueChanged.connect(psd_pre_trigger.setValue)

        self.set_connected_controls_enabled(False)

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def set_connected_controls_enabled(self, enabled: bool) -> None:
        self.live_view.set_controls_enabled(enabled)
        self.scope_view.set_controls_enabled(enabled)
        self.config_panel.set_controls_enabled(enabled)
        self.chk_record.setEnabled(enabled)

    def set_recording_active(self, active: bool) -> None:
        """Makes recording state hard to miss: a colored dot + label on the checkbox itself, and a
        tinted background across the whole window -- not just a label change buried in a status
        bar the user has to go looking for."""
        if active:
            self.central_widget.setStyleSheet("background-color: #4a1414;")
            self.chk_record.setStyleSheet(
                "QCheckBox { font-weight: bold; padding: 2px 8px; color: #ff5555; }"
            )
        else:
            self.central_widget.setStyleSheet("")
            self.chk_record.setStyleSheet("QCheckBox { font-weight: bold; padding: 2px 8px; }")
