"""Raw-trace oscilloscope view, with traditional Start/Stop/Single controls -- for setting up
acquisition parameters (trigger threshold, gate placement) by eye. "Start" continuously re-captures
and redraws (like a real scope's run mode); "Single" grabs and holds exactly one frame; neither
writes anything to disk -- this view is for live setup, not for recording (see
CLI_documentation.md section 2.6, $RT).

There used to be a separate "Samples" spinbox here controlling the `n` argument to $RT. That was
misleading: $RT's `n` only CAPS how many samples of the trigger's last completed background
capture get returned (Bringup_CaptureTrace() in firmware) -- it does not re-arm a new capture at
that depth. The trace's actual length is entirely governed by the Trigger's own Depth register
(TRIGGER_FIELDS below, embedded in this same view), so asking $RT for more than Depth currently
holds can never return more than Depth. Every request here now simply asks for the firmware's own
max (TRACE_MAX_SAMPLES) so this cap is never the limiting factor -- change Depth, not a second
"Samples" control, to actually get a longer or shorter trace.

All controls for this view live inside ScopeView itself, not in MainWindow -- nothing here needs
anything from outside this widget except the client (set_client(), for the embedded Trigger config
form) and the events fed in via show_trace().
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from fci_api import FciClient, TraceResult

from .config_panel import TRACE_MAX_SAMPLES, SubsystemPanel, TRIGGER_FIELDS


class ScopeView(QWidget):
    start_clicked = Signal(int)  # requested sample count
    stop_clicked = Signal()
    single_clicked = Signal(int)  # requested sample count
    calibrate_clicked = Signal()

    def __init__(self):
        super().__init__()
        self._running = False
        self.confirm_start = None
        """Optional callable, injected by the controller: () -> bool. See LiveView's identical
        attribute for why -- consulted only by Start (continuous run), not Single, matching what
        was actually asked for."""
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        ctrl_box = QGroupBox("Oscilloscope Controls")
        ctrl_layout = QHBoxLayout(ctrl_box)

        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_single = QPushButton("Single")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_single.clicked.connect(self._on_single)
        ctrl_layout.addWidget(self.btn_start)
        ctrl_layout.addWidget(self.btn_stop)
        ctrl_layout.addWidget(self.btn_single)

        self.btn_calibrate = QPushButton("Calibrate Threshold...")
        self.btn_calibrate.clicked.connect(self.calibrate_clicked.emit)
        ctrl_layout.addWidget(self.btn_calibrate)

        self.lbl_status = QLabel("No trace captured yet")
        ctrl_layout.addWidget(self.lbl_status, stretch=1)
        layout.addWidget(ctrl_box)

        self.trigger_config = SubsystemPanel("Trigger Configuration", TRIGGER_FIELDS,
                                              "get_trigger", "set_trigger")
        self.trigger_config.config_changed.connect(self._on_trigger_config_changed)
        layout.addWidget(self.trigger_config)

        self.plot_widget = pg.PlotWidget(title="Raw trace (signed ADC codes)")
        self.plot_widget.setLabel("bottom", "sample index")
        self.plot_widget.setLabel("left", "ADC code")
        self.plot_widget.showGrid(x=True, y=True)
        self.curve = self.plot_widget.plot(pen=pg.mkPen("c", width=1))

        self.trigger_line = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen("r", width=1, style=Qt.PenStyle.DashLine),
            label="trigger threshold = {value:0.0f}",
            labelOpts={"position": 0.98, "color": "r"},
        )
        self.trigger_line.setVisible(False)
        self.plot_widget.addItem(self.trigger_line)

        layout.addWidget(self.plot_widget, stretch=1)

    # ---- internal button handlers: local enable/disable state, then tell the controller ----

    def _on_start(self) -> None:
        if self.confirm_start is not None and not self.confirm_start():
            return
        self._running = True
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_single.setEnabled(False)
        self.start_clicked.emit(TRACE_MAX_SAMPLES)

    def _on_stop(self) -> None:
        self._running = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_single.setEnabled(True)
        self.stop_clicked.emit()

    def _on_single(self) -> None:
        self.lbl_status.setText("Capturing...")
        self.single_clicked.emit(TRACE_MAX_SAMPLES)

    def _on_trigger_config_changed(self, cfg) -> None:
        self.set_trigger_level(cfg.threshold)

    def set_client(self, client: FciClient | None) -> None:
        self.trigger_config.set_client(client)

    def set_controls_enabled(self, enabled: bool) -> None:
        if not enabled and self._running:
            self._on_stop()
        self.btn_start.setEnabled(enabled)
        self.btn_single.setEnabled(enabled)
        self.btn_stop.setEnabled(enabled and self._running)
        self.btn_calibrate.setEnabled(enabled)

    def set_trigger_level(self, threshold: int | None) -> None:
        """Updates the horizontal dashed reference line. None hides it (e.g. while disconnected,
        or if the trigger config couldn't be read)."""
        if threshold is None:
            self.trigger_line.setVisible(False)
            return
        self.trigger_line.setPos(threshold)
        self.trigger_line.setVisible(True)

    def show_trace(self, trace: TraceResult | None) -> None:
        if trace is None:
            self.lbl_status.setText("No trace captured yet (device has no completed capture)")
            return
        self.curve.setData(list(range(len(trace.samples))), trace.samples)
        self.lbl_status.setText(f"{len(trace.samples)} samples" + (" (running)" if self._running else ""))
