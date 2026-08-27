"""AppController: wires MainWindow's UI signals to AcquisitionWorker requests, and worker signals
back to widget updates. Owns the connection lifecycle (including a simple reconnect-on-unexpected-
drop state machine, mirroring the reference GUI's own) and the CSV logger.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QFileDialog

import config
from acquisition_worker import AcquisitionWorker
from csv_logger import CsvLogger
from fci_api import FciClient, FciTransport, list_matching_ports

logger = logging.getLogger(__name__)


class AppController(QObject):
    def __init__(self, view):
        super().__init__()
        self.view = view

        self.transport: FciTransport | None = None
        self.config_client: FciClient | None = None
        self.worker: AcquisitionWorker | None = None
        self.csv_logger: CsvLogger | None = None

        self.is_connected = False
        self._expect_connection = False
        self._reconnect_count = 0

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._attempt_reconnect)

        self._connect_signals()
        self.scan_ports()
        self.view.txt_csv_dir.setText(str(config.DEFAULT_CSV_DIR))

        logger.info("AppController initialized.")

    def _connect_signals(self) -> None:
        self.view.btn_refresh_ports.clicked.connect(self.scan_ports)
        self.view.btn_connect.clicked.connect(self.toggle_connection)
        self.view.btn_browse_dir.clicked.connect(self.browse_csv_dir)
        self.view.live_view.start_clicked.connect(self.start_acquisition)
        self.view.live_view.stop_clicked.connect(self.stop_acquisition)
        self.view.scope_view.start_clicked.connect(self.scope_start)
        self.view.scope_view.stop_clicked.connect(self.scope_stop)
        self.view.scope_view.single_clicked.connect(self.scope_single)

    # ---------------------------------------------------------------------------- port discovery

    def scan_ports(self) -> None:
        self.view.combo_ports.clear()
        matches = list_matching_ports(config.TARGET_VID_HEX, config.TARGET_PID_HEX)
        for p in matches:
            label = f"{p.device} ({p.description})" if p.description else p.device
            self.view.combo_ports.addItem(label, p.device)
        if not matches:
            self.view.combo_ports.addItem(
                f"No device found (VID:PID {config.TARGET_VID_HEX}:{config.TARGET_PID_HEX})", None
            )
            self.view.btn_connect.setEnabled(False)
        else:
            self.view.btn_connect.setEnabled(True)

    # ---------------------------------------------------------------------------- connect/disconnect

    def toggle_connection(self) -> None:
        if not self.is_connected:
            self._connect()
        else:
            self._disconnect(user_requested=True)

    def _connect(self) -> None:
        port = self.view.combo_ports.currentData()
        if not port:
            return
        self._expect_connection = True
        self._reconnect_count = 0
        self._reconnect_timer.stop()

        self.transport = FciTransport(port)
        self.config_client = FciClient(self.transport)
        self.worker = AcquisitionWorker(self.transport, config.BATCH_POLL_INTERVAL_MS / 1000.0,
                                         config.STATS_POLL_INTERVAL_MS / 1000.0)
        self.worker.batch_received.connect(self.on_batch_received)
        self.worker.trace_received.connect(self.view.scope_view.show_trace)
        self.worker.stats_received.connect(self.view.live_view.update_stats)
        self.worker.connection_changed.connect(self.on_connection_changed)
        self.worker.acquisition_state_changed.connect(self.on_acquisition_state_changed)
        self.worker.error_occurred.connect(self.on_error)

        self.view.btn_connect.setText("Connecting...")
        self.view.btn_connect.setEnabled(False)
        self.view.combo_ports.setEnabled(False)
        self.view.btn_refresh_ports.setEnabled(False)
        self.worker.start()

    def _disconnect(self, user_requested: bool) -> None:
        if user_requested:
            self._expect_connection = False
            self._reconnect_count = 0
            self._reconnect_timer.stop()

        self.view.btn_connect.setText("Disconnecting...")
        self.view.btn_connect.setEnabled(False)
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()  # blocks briefly (bounded by the transport timeout); see docstring

    def _attempt_reconnect(self) -> None:
        if not self._expect_connection or self.is_connected:
            return
        port = self.view.combo_ports.currentData()
        if not port or (self.worker is not None and self.worker.isRunning()):
            return
        self._reconnect_count += 1
        logger.warning(
            f"Reconnect attempt {self._reconnect_count}/{config.MAX_RECONNECT_ATTEMPTS} to {port}"
        )
        self.view.btn_connect.setText(f"Retrying ({self._reconnect_count}/"
                                       f"{config.MAX_RECONNECT_ATTEMPTS})...")
        self._connect()

    @Slot(bool)
    def on_connection_changed(self, online: bool) -> None:
        self.is_connected = online
        if online:
            self._reconnect_timer.stop()
            self._reconnect_count = 0
            self.view.lbl_status.setText("\U0001F7E2 Connected")
            self.view.btn_connect.setText("Disconnect")
            self.view.btn_connect.setEnabled(True)
            self.view.set_connected_controls_enabled(True)
            self.view.config_panel.set_client(self.config_client)
            self._refresh_scope_trigger_level()
        else:
            self.view.set_connected_controls_enabled(False)
            self.view.config_panel.set_client(None)
            self.view.live_view.set_controls_enabled(False)
            self.view.scope_view.set_trigger_level(None)
            self.csv_logger = None
            self.worker = None
            self.transport = None
            self.config_client = None

            if self._expect_connection:
                if self._reconnect_count >= config.MAX_RECONNECT_ATTEMPTS:
                    self.view.lbl_status.setText("\U0001F534 Link lost (giving up)")
                    self.view.btn_connect.setText("Connect")
                    self.view.btn_connect.setEnabled(True)
                    self.view.combo_ports.setEnabled(True)
                    self.view.btn_refresh_ports.setEnabled(True)
                    self._expect_connection = False
                else:
                    self.view.lbl_status.setText("\U0001F7E0 Link lost, retrying...")
                    self._reconnect_timer.start(config.RECONNECT_INTERVAL_MS)
            else:
                self.view.lbl_status.setText("\U0001F534 Disconnected")
                self.view.btn_connect.setText("Connect")
                self.view.btn_connect.setEnabled(True)
                self.view.combo_ports.setEnabled(True)
                self.view.btn_refresh_ports.setEnabled(True)

    @Slot(str)
    def on_error(self, message: str) -> None:
        logger.warning(message)
        self.view.lbl_status.setText(f"⚠ {message[:70]}")

    # ---------------------------------------------------------------------------------- acquisition

    def start_acquisition(self) -> None:
        if self.worker is not None:
            self.worker.request_start_acquisition()

    def stop_acquisition(self) -> None:
        if self.worker is not None:
            self.worker.request_stop_acquisition()

    @Slot(bool)
    def on_acquisition_state_changed(self, enabled: bool) -> None:
        if enabled:
            self.view.live_view.clear()
            self.csv_logger = CsvLogger(Path(self.view.txt_csv_dir.text()))
            logger.info(f"CSV logging to {self.csv_logger.path}")
        else:
            self.csv_logger = None

    @Slot(list)
    def on_batch_received(self, events) -> None:
        self.view.live_view.add_events(events)
        if self.csv_logger is not None:
            self.csv_logger.append_many(events)

    # ---------------------------------------------------------------------------------- scope

    def scope_start(self, n: int) -> None:
        self._refresh_scope_trigger_level()
        if self.worker is not None:
            self.worker.request_scope_start(n)

    def scope_stop(self) -> None:
        if self.worker is not None:
            self.worker.request_scope_stop()

    def scope_single(self, n: int) -> None:
        self._refresh_scope_trigger_level()
        if self.worker is not None:
            self.worker.request_trace(n)

    def _refresh_scope_trigger_level(self) -> None:
        """Fetches the current trigger threshold directly (not routed through the worker -- see
        AcquisitionWorker's docstring for why config reads are the one thing allowed to bypass it)
        and pushes it to the scope view's dashed reference line.

        Called once per Start/Single click rather than once per displayed frame: the threshold
        rarely changes mid-session, and re-fetching it on every continuous-scope frame would add a
        second round trip alongside every $RT for no benefit most of the time.
        """
        if self.config_client is None:
            return
        try:
            threshold = self.config_client.get_trigger().threshold
        except Exception as e:
            # Deliberately broad: transact() can raise a raw pyserial exception (not just
            # FciError) if the link drops mid-call, and this is a best-effort UI refresh, not a
            # critical path -- worst case the dashed line stays at its last known value.
            logger.warning(f"could not read trigger threshold for the scope line: {e}")
            return
        self.view.scope_view.set_trigger_level(threshold)

    # -------------------------------------------------------------------------------------- misc

    def browse_csv_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self.view, "Select CSV Output Directory", self.view.txt_csv_dir.text()
        )
        if chosen:
            self.view.txt_csv_dir.setText(chosen)

    def cleanup(self) -> None:
        logger.info("Shutting down.")
        self._expect_connection = False
        self._reconnect_timer.stop()
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
