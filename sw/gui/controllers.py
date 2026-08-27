"""AppController: wires MainWindow's UI signals to AcquisitionWorker requests, and worker signals
back to widget updates. Owns the connection lifecycle (including a simple reconnect-on-unexpected-
drop state machine, mirroring the reference GUI's own) and the CSV logger.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

import config
from acquisition_worker import AcquisitionWorker
from csv_logger import CsvLogger, TraceCsvLogger
from fci_api import FciClient, FciError, FciTransport, list_matching_ports
from ui.calibration_wizard import CalibrationWizard
from ui.fom_wizard import FomWizard

logger = logging.getLogger(__name__)


class AppController(QObject):
    def __init__(self, view):
        super().__init__()
        self.view = view

        self.transport: FciTransport | None = None
        self.config_client: FciClient | None = None
        self.worker: AcquisitionWorker | None = None
        self.csv_logger: CsvLogger | None = None
        self.scope_csv_logger: TraceCsvLogger | None = None

        self.is_connected = False
        self._expect_connection = False
        self._reconnect_count = 0

        self._live_acq_running = False
        self._scope_running = False
        """Tracked here (not read off the views) so on_record_toggled() can tell whether
        re-checking Record mid-acquisition should resume recording immediately -- see there."""

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._attempt_reconnect)

        self._connect_signals()
        self.scan_ports()
        self.view.txt_csv_dir.setText(str(config.DEFAULT_CSV_DIR))
        self.view.txt_file_prefix.setText(time.strftime("%Y%m%d"))
        self._update_filename_preview()

        logger.info("AppController initialized.")

    def _connect_signals(self) -> None:
        self.view.btn_refresh_ports.clicked.connect(self.scan_ports)
        self.view.btn_connect.clicked.connect(self.toggle_connection)
        self.view.btn_browse_dir.clicked.connect(self.browse_csv_dir)
        self.view.chk_record.toggled.connect(self.on_record_toggled)
        self.view.txt_file_prefix.textChanged.connect(self._update_filename_preview)
        self.view.chk_autoincrement.toggled.connect(self._update_filename_preview)
        self.view.live_view.confirm_start = self._confirm_and_maybe_record
        self.view.scope_view.confirm_start = self._confirm_and_maybe_record
        self.view.live_view.start_clicked.connect(self.start_acquisition)
        self.view.live_view.stop_clicked.connect(self.stop_acquisition)
        self.view.scope_view.start_clicked.connect(self.scope_start)
        self.view.scope_view.stop_clicked.connect(self.scope_stop)
        self.view.scope_view.single_clicked.connect(self.scope_single)
        self.view.scope_view.calibrate_clicked.connect(self.open_calibration_wizard)
        self.view.live_view.fom_wizard_clicked.connect(self.open_fom_wizard)

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
        self.worker.trace_received.connect(self.on_trace_received)
        self.worker.stats_received.connect(self.view.live_view.update_stats)
        self.worker.connection_changed.connect(self.on_connection_changed)
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
            self.view.live_view.set_client(self.config_client)
            self.view.scope_view.set_client(self.config_client)
        else:
            self.view.set_connected_controls_enabled(False)
            self.view.config_panel.set_client(None)
            self.view.live_view.set_client(None)
            self.view.live_view.set_controls_enabled(False)
            self.view.scope_view.set_client(None)
            self.view.scope_view.set_trigger_level(None)
            # Closes out any in-progress recording session directly, without touching the Record
            # checkbox itself -- it's a lasting preference (armed by default), not something a
            # disconnect should reset, so it stays exactly as the user left it for next time.
            if self.csv_logger is not None:
                logger.info(f"Recording stopped by disconnect ({self.csv_logger.event_count} "
                            f"events logged).")
            self.csv_logger = None
            self.scope_csv_logger = None
            self.view.set_recording_active(False)
            self.worker = None
            self.transport = None
            self.config_client = None
            self._live_acq_running = False
            self._scope_running = False

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
        self._live_acq_running = True

    def stop_acquisition(self) -> None:
        if self.worker is not None:
            self.worker.request_stop_acquisition()
        self._live_acq_running = False

    @Slot(list)
    def on_batch_received(self, events) -> None:
        self.view.live_view.add_events(events)
        if self.csv_logger is not None:
            self.csv_logger.append_many(self.view.live_view.filter_for_recording(events))

    @Slot(object)
    def on_trace_received(self, trace) -> None:
        self.view.scope_view.show_trace(trace)
        if self.scope_csv_logger is not None and trace is not None:
            self.scope_csv_logger.append(trace)

    # ---------------------------------------------------------------------------------- recording

    def on_record_toggled(self, checked: bool) -> None:
        """The Record checkbox is a standing preference (armed by default), not itself the
        trigger for writing files -- that happens in _ensure_recording_session(), gated behind
        the confirmation shown when Start is actually pressed. Unchecking it, though, closes any
        session already in progress immediately: turning Record off must always mean "stop
        writing now", not "stop next time Start happens to be pressed".

        Symmetrically, re-checking it while live/scope acquisition is already running (Start was
        pressed before Record got toggled off) has no future Start press to hang a fresh
        _ensure_recording_session() off of -- resume immediately instead. Skips the confirmation
        dialog on purpose: that dialog guards *starting* acquisition with recording armed, not
        re-arming a preference the user just disabled a moment ago on already-running acquisition."""
        if not checked and self.csv_logger is not None:
            logger.info(f"Recording stopped ({self.csv_logger.event_count} events logged).")
            self.csv_logger = None
            self.scope_csv_logger = None
            self.view.set_recording_active(False)
            self._update_filename_preview()
        elif checked and self.csv_logger is None and (self._live_acq_running or self._scope_running):
            if not self._ensure_recording_session():
                self.view.chk_record.setChecked(False)  # user declined the overwrite warning

    def _confirm_and_maybe_record(self) -> bool:
        """Consulted by LiveView/ScopeView before their Start button does anything. Returns
        whether Start should proceed at all -- declining the recording warning (or a follow-on
        overwrite warning from _ensure_recording_session()) cancels the whole Start action, not
        just recording, since the warning is framed as "Start will begin recording" rather than
        as a separate, skippable prompt."""
        if not self.view.chk_record.isChecked():
            return True
        reply = QMessageBox.question(
            self.view,
            "Start Acquisition",
            "Recording is enabled -- starting will begin writing data to CSV "
            f"({self.view.txt_csv_dir.text()}).\n\nContinue?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return False
        return self._ensure_recording_session()

    # ---- filename prefix / index (File Management tab) ----

    FILENAME_INDEX_DIGITS = 4

    def _sanitized_prefix(self) -> str:
        prefix = self.view.txt_file_prefix.text().strip().replace("/", "_").replace("\\", "_")
        return prefix or "recording"

    def _next_free_index(self, out_dir: Path, prefix: str) -> int:
        """Lowest index for which neither the fci_live nor the scope_traces file exists yet --
        1 if out_dir doesn't exist or nothing matching `prefix` is in it."""
        used: set[int] = set()
        if out_dir.exists():
            pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)_(?:fci_live|scope_traces)\.csv$")
            for p in out_dir.iterdir():
                m = pattern.match(p.name)
                if m:
                    used.add(int(m.group(1)))
        index = 1
        while index in used:
            index += 1
        return index

    def _would_overwrite(self, out_dir: Path, prefix: str, index: int) -> bool:
        stem = f"{prefix}_{index:0{self.FILENAME_INDEX_DIGITS}d}"
        return (out_dir / f"{stem}_fci_live.csv").exists() or \
               (out_dir / f"{stem}_scope_traces.csv").exists()

    def _update_filename_preview(self) -> None:
        out_dir = Path(self.view.txt_csv_dir.text())
        prefix = self._sanitized_prefix()
        autoincrement = self.view.chk_autoincrement.isChecked()
        index = self._next_free_index(out_dir, prefix) if autoincrement else 1
        stem = f"{prefix}_{index:0{self.FILENAME_INDEX_DIGITS}d}"
        warning = ""
        if not autoincrement and self._would_overwrite(out_dir, prefix, index):
            warning = "  ⚠ already exists -- will be overwritten"
        self.view.lbl_filename_preview.setText(
            f"Next files: {stem}_fci_live.csv, {stem}_scope_traces.csv{warning}"
        )

    def _ensure_recording_session(self) -> bool:
        """Returns whether a session is (now) active. False only means the user declined an
        overwrite warning -- callers (Start confirmation, on_record_toggled's resume-mid-run
        path) must treat that the same as declining to record at all."""
        if self.csv_logger is not None:
            return True  # already recording (e.g. Stop then Start again) -- keep the same files
        out_dir = Path(self.view.txt_csv_dir.text())
        prefix = self._sanitized_prefix()
        autoincrement = self.view.chk_autoincrement.isChecked()

        if autoincrement:
            index = self._next_free_index(out_dir, prefix)
        else:
            index = 1
            if self._would_overwrite(out_dir, prefix, index):
                stem = f"{prefix}_{index:0{self.FILENAME_INDEX_DIGITS}d}"
                reply = QMessageBox.warning(
                    self.view,
                    "File Will Be Overwritten",
                    f"{stem}_fci_live.csv and/or {stem}_scope_traces.csv already exist in "
                    f"{out_dir} and Autoincrement is off -- continuing will overwrite them."
                    "\n\nContinue?",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if reply != QMessageBox.StandardButton.Ok:
                    return False

        self.csv_logger = CsvLogger(out_dir, prefix, index)
        self.scope_csv_logger = TraceCsvLogger(out_dir, prefix, index)
        self.view.set_recording_active(True)
        logger.info(f"Recording started: {self.csv_logger.path}, {self.scope_csv_logger.path}")
        self._update_filename_preview()
        return True

    # ---------------------------------------------------------------------------------- scope

    def scope_start(self, n: int) -> None:
        if self.worker is not None:
            self.worker.request_scope_start(n)
        self._scope_running = True

    def scope_stop(self) -> None:
        if self.worker is not None:
            self.worker.request_scope_stop()
        self._scope_running = False

    def scope_single(self, n: int) -> None:
        if self.worker is not None:
            self.worker.request_trace(n)

    def open_calibration_wizard(self) -> None:
        if self.config_client is None:
            return
        dlg = CalibrationWizard(self.config_client, self.view)
        if dlg.exec() != CalibrationWizard.DialogCode.Accepted:
            return
        try:
            dlg.apply_to_device()
        except FciError as e:
            logger.warning(f"calibration apply failed: {e}")
            QMessageBox.warning(self.view, "Apply Failed", f"Could not write trigger config: {e}")
            return
        self.view.scope_view.trigger_config.refresh()

    def open_fom_wizard(self) -> None:
        dlg = FomWizard(self.config_client, self.worker, self.view.live_view.get_accumulated_events,
                         self.view)
        dlg.exec()

    # -------------------------------------------------------------------------------------- misc

    def browse_csv_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self.view, "Select CSV Output Directory", self.view.txt_csv_dir.text()
        )
        if chosen:
            self.view.txt_csv_dir.setText(chosen)
            self._update_filename_preview()

    def cleanup(self) -> None:
        logger.info("Shutting down.")
        self._expect_connection = False
        self._reconnect_timer.stop()
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
