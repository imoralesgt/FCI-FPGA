"""This view is the front end of the whole capture chain, not a separate viewer: trigger_core's
live-triggered stream feeds this trace AND fci_core/psd_core directly (same Vivado BD tap, via
axis_broadcaster_0) -- the Trigger config here is what every FCI/PSD event is actually computed
from, not just what gets plotted. "Start" continuously re-captures and redraws; "Single" grabs one
frame; neither writes anything to disk (see CLI_documentation.md section 2.6, $RT).

$RT's `n` only CAPS how many samples of the trigger's last completed capture come back
(Bringup_CaptureTrace() in firmware); it does not re-arm a new capture at that depth. The trace's
actual length is set entirely by the Trigger's own Depth field below. Every request here asks for
the firmware's own max (TRACE_MAX_SAMPLES), so Depth is always the real limiting factor.

All controls for this view live inside ScopeView itself, not in MainWindow -- nothing here needs
anything from outside this widget except the client (set_client(), for the embedded Trigger config
form) and the events fed in via show_trace().
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from fci_api import FciClient, TraceResult

from .config_panel import TRACE_MAX_SAMPLES, SubsystemPanel, TRIGGER_FIELDS


class ScopeView(QWidget):
    start_clicked = Signal(int)  # requested sample count
    stop_clicked = Signal()
    single_clicked = Signal(int)  # requested sample count
    calibrate_clicked = Signal()
    diagnostics_clicked = Signal()

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

        layout.addWidget(QLabel("Sets acquisition settings for FCI/PSD"))

        ctrl_box = QGroupBox("Capture Controls")
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

        self.btn_diagnostics = QPushButton("Autodiagnostics...")
        self.btn_diagnostics.setToolTip(
            "Runs the device's self-diagnostics ($DG): threshold calibration, a live event through "
            "the FCI core and a raw-trace capture -- the checks that used to run at every power-on. "
            "Takes up to ~15 s and needs a few real events, so keep sources away from the detector "
            "for a meaningful threshold. Stops acquisition; the trigger settings are restored "
            "afterwards and the calibrated threshold is only offered, not applied.")
        self.btn_diagnostics.clicked.connect(self.diagnostics_clicked.emit)
        ctrl_layout.addWidget(self.btn_diagnostics)

        self.lbl_status = QLabel("No trace captured yet")
        ctrl_layout.addWidget(self.lbl_status, stretch=1)
        layout.addWidget(ctrl_box)

        # Host-side, not a register: the device only FLAGS pile-up (the two fields above); whether
        # flagged events are kept is decided in the Live FCI/PSD view, which filters by this box
        # (MainWindow binds it there). Shown here, beside the window and threshold it depends on.
        self.chk_reject_pileup = QCheckBox()
        # The pile-up threshold, shown relative to the trigger threshold. The device takes counts
        # (the hidden pileup_threshold field, which Apply and the project carry); this spin box is
        # a view onto it: counts = ratio x |trigger threshold|, recomputed whenever either changes.
        self.spin_pileup_ratio = QDoubleSpinBox()
        self.spin_pileup_ratio.setRange(0.0, 50.0)
        self.spin_pileup_ratio.setDecimals(2)
        self.spin_pileup_ratio.setSingleStep(0.1)
        self.spin_pileup_ratio.setSuffix(" x")
        self.spin_pileup_ratio.setValue(1.0)
        self.trigger_config = SubsystemPanel(
            "Trigger Configuration", TRIGGER_FIELDS, "get_trigger", "set_trigger",
            extra_rows=(("Pile-up threshold (x trigger)", self.spin_pileup_ratio,
                         "Rise that counts as a second pulse, as a multiple of the trigger "
                         "threshold; the device receives the absolute value in ADC counts "
                         "(shown on the plot as the dashed 'pile-up rise' line). The rise is "
                         "s[n] - s[n-(CFD delay+1)], tested once the first pulse has started to "
                         "fall: a jump, wherever it starts. About 2x flagged 0.02% (OGS) and 0.25% "
                         "(CLYC) of clean events while catching 90%+ of second pulses above that "
                         "size (project log, section 11).", "Pile-Up"),
                        ("Reject pile-up", self.chk_reject_pileup,
                         "Drops events flagged as pile-up (a second pulse within the pile-up "
                         "window) from the Live FCI/PSD plots, counts and recorded file, from now "
                         "on -- events already plotted stay. Needs a non-zero pile-up window. The "
                         "flagged count is under Live FCI/PSD > Advanced either way, and the "
                         "file's `cuts:` line records this setting. Not a device register: Apply "
                         "does not send it, and it is locked while recording.", "Pile-Up"),))
        self.trigger_config.config_changed.connect(self._on_trigger_config_changed)
        ctl = self.trigger_config._controls
        self._syncing_pileup = False
        self.spin_pileup_ratio.valueChanged.connect(self._pileup_counts_from_ratio)
        ctl["threshold"].valueChanged.connect(self._pileup_counts_from_ratio)
        # Counts set from outside (a device read, a project load): show them as a ratio.
        ctl["pileup_threshold"].valueChanged.connect(self._pileup_ratio_from_counts)
        # The pile-up line follows the panel as it is edited, like the controls it depicts --
        # placed from the last device read instead, it stayed on the old side of the baseline
        # after Rising edge was toggled and before Apply.
        for name in ("pileup_threshold", "pileup_window"):
            ctl[name].valueChanged.connect(self._update_pileup_line)
        ctl["rising"].toggled.connect(self._update_pileup_line)
        layout.addWidget(self.trigger_config)

        self.plot_widget = pg.PlotWidget(title="Raw trace (signed ADC codes)")
        self.plot_widget.setLabel("bottom", "sample index")
        self.plot_widget.setLabel("left", "ADC code")
        self.plot_widget.showGrid(x=True, y=True)
        # Fixed Y range rather than autoscaling per trace: a single clipped/saturated capture
        # (project log section 8k) would otherwise yank the axis out to ~14500 and make every
        # normal-amplitude trace after it look flat. -300..8000 covers this detector's normal
        # pulse range with headroom; setYRange also switches the axis out of autorange mode, and
        # nothing later in this view calls autoRange() to switch it back.
        self.plot_widget.setYRange(-300, 8000, padding=0)
        self.curve = self.plot_widget.plot(pen=pg.mkPen((0, 200, 120), width=1))  # Spectrum tab green

        TRIGGER_LINE_COLOR = "#8ECBF9"
        self.trigger_line = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen(TRIGGER_LINE_COLOR, width=1, style=Qt.PenStyle.DashLine),
            label="trigger threshold = {value:0.0f}",
            labelOpts={"position": 0.98, "color": TRIGGER_LINE_COLOR},
        )
        self.trigger_line.setVisible(False)
        self.plot_widget.addItem(self.trigger_line)

        # Pile-up: the flag tests a RISE over the CFD delay, not a level, so no horizontal line
        # can mark it. Shown instead: the window the test covers (trigger sample to trigger + W),
        # and on a frame the board flagged, the sample where the rise first exceeded the
        # threshold. Both use the settings last read from the device, which the trace reflects.
        PILEUP_COLOR = "#FC8D59"
        self.pileup_region = pg.LinearRegionItem(
            movable=False, brush=pg.mkBrush(252, 141, 89, 30), pen=pg.mkPen(None))
        self.pileup_region.setZValue(-10)
        self.pileup_region.setVisible(False)
        self.plot_widget.addItem(self.pileup_region)
        self.pileup_marker = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(PILEUP_COLOR, width=1, style=Qt.PenStyle.DashLine),
            label="pile-up", labelOpts={"position": 0.95, "color": PILEUP_COLOR})
        self.pileup_marker.setVisible(False)
        self.plot_widget.addItem(self.pileup_marker)
        # The pile-up threshold as a size: a second pulse is flagged when it RISES at least this
        # much, wherever it starts, so the line is drawn that far from the baseline (0) -- the
        # height a second pulse arriving on a quiet baseline would have to reach.
        self.pileup_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(PILEUP_COLOR, width=1, style=Qt.PenStyle.DashLine),
            label="pile-up rise = {value:0.0f}",
            labelOpts={"position": 0.85, "color": PILEUP_COLOR})
        self.pileup_line.setVisible(False)
        self.plot_widget.addItem(self.pileup_line)
        self._trigger_cfg = None

        layout.addWidget(self.plot_widget, stretch=1)

    def project_settings(self) -> dict:
        """Trigger tab host state a project keeps (Project.trigger): the pile-up threshold as a
        multiple of the trigger threshold. Rounded to the spin box's own precision."""
        return {"pileup_ratio": round(self.spin_pileup_ratio.value(), 2)}

    def apply_project_settings(self, settings: dict) -> None:
        ratio = settings.get("pileup_ratio")
        if isinstance(ratio, (int, float)) and 0.0 <= float(ratio) <= self.spin_pileup_ratio.maximum():
            self.spin_pileup_ratio.setValue(float(ratio))

    def pileup_settings_line(self) -> str:
        """One line for the RAW and LIST headers saying what the pile-up flag (the `pileup`
        columns) means for this recording, from the settings in the Trigger tab -- which a
        recording starts from, since the project is applied and saved first."""
        ctl = self.trigger_config._controls
        if not ctl["pileup_threshold"].isEnabled():
            return "pileup: not available (bitstream without the pile-up flag)"
        window = ctl["pileup_window"].value()
        reject = "on" if self.chk_reject_pileup.isChecked() else "off"
        if window == 0:
            return f"pileup: flag off (window 0), rejection={reject}"
        return (f"pileup: window={window} samples, threshold={ctl['pileup_threshold'].value()} "
                f"ADC counts ({self.spin_pileup_ratio.value():.2f} x trigger threshold), "
                f"rejection={reject}  [pileup=1 if s[n]-s[n-(cfd_delay+1)] (sign-flipped for "
                f"falling edges) exceeds the threshold after the pulse starts to fall, within "
                f"`window` samples of the trigger; rejection drops flagged events from the LIST "
                f"file]")

    def _update_pileup_line(self, *_) -> None:
        """Draws the pile-up threshold as a size from the baseline, on the pulse's side: +counts
        for rising pulses, -counts for falling ones. Shown while the window is non-zero on a
        bitstream that has the pile-up flag."""
        ctl = self.trigger_config._controls
        counts = ctl["pileup_threshold"].value()
        on = ctl["pileup_window"].value() > 0 and ctl["pileup_threshold"].isEnabled()
        self.pileup_line.setPos(counts if ctl["rising"].isChecked() else -counts)
        self.pileup_line.setVisible(on)

    def _pileup_counts_from_ratio(self, *_) -> None:
        """Ratio or trigger threshold changed: recompute the counts the device will receive."""
        if self._syncing_pileup:
            return
        ctl = self.trigger_config._controls
        counts = round(self.spin_pileup_ratio.value() * abs(ctl["threshold"].value()))
        self._syncing_pileup = True
        try:
            ctl["pileup_threshold"].setValue(max(0, min(32767, counts)))
        finally:
            self._syncing_pileup = False

    def _pileup_ratio_from_counts(self, counts: int) -> None:
        """Counts set from the device or a project: show them relative to the trigger threshold."""
        if self._syncing_pileup:
            return
        threshold = abs(self.trigger_config._controls["threshold"].value())
        if threshold == 0:
            return
        self._syncing_pileup = True
        try:
            self.spin_pileup_ratio.setValue(counts / threshold)
        finally:
            self._syncing_pileup = False

    # ---- internal button handlers: local enable/disable state, then tell the controller ----

    def _on_start(self) -> None:
        if self.confirm_start is not None and not self.confirm_start():
            return
        self._running = True
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_single.setEnabled(False)
        # Calibration widens/restores the trigger's delay+depth registers around its capture
        # loop. Doing that concurrently with this view's own continuous $RT polling races the
        # same trigger-core/DMA registers the live capture is using and has been observed to hang
        # the device -- so calibration is simply unavailable while a run is active.
        self.btn_calibrate.setEnabled(False)
        self.btn_diagnostics.setEnabled(False)
        self.start_clicked.emit(TRACE_MAX_SAMPLES)

    def _on_stop(self) -> None:
        self._running = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_single.setEnabled(True)
        self.btn_calibrate.setEnabled(True)
        self.btn_diagnostics.setEnabled(True)
        self.stop_clicked.emit()

    def _on_single(self) -> None:
        self.lbl_status.setText("Capturing...")
        self.single_clicked.emit(TRACE_MAX_SAMPLES)

    def _on_trigger_config_changed(self, cfg) -> None:
        self.set_trigger_level(cfg.threshold)
        self._trigger_cfg = cfg
        window = getattr(cfg, "pileup_window", None) or 0
        if window > 0:
            # The trigger sits at frame sample `delay`; the window cannot run past the frame.
            end = min(cfg.delay + window, cfg.depth - 1)
            self.pileup_region.setRegion((cfg.delay, end))
        self.pileup_region.setVisible(window > 0)
        self._update_pileup_line()
        self.spin_pileup_ratio.setEnabled(
            self.trigger_config._controls["pileup_threshold"].isEnabled())


    def set_client(self, client: FciClient | None) -> None:
        self.trigger_config.set_client(client)
        self.spin_pileup_ratio.setEnabled(
            self.trigger_config._controls["pileup_threshold"].isEnabled())

    def set_controls_enabled(self, enabled: bool) -> None:
        if not enabled and self._running:
            self._on_stop()
        self.btn_start.setEnabled(enabled)
        self.btn_single.setEnabled(enabled)
        self.btn_stop.setEnabled(enabled and self._running)
        self.btn_calibrate.setEnabled(enabled and not self._running)
        self.btn_diagnostics.setEnabled(enabled and not self._running)

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
        status = f"{len(trace.samples)} samples"
        at = None
        if trace.pileup:
            at = self._pileup_sample(trace.samples)
            status += " -- pile-up flagged" + (f" at sample {at}" if at is not None else "")
        if at is not None:
            self.pileup_marker.setPos(at)
        self.pileup_marker.setVisible(at is not None)
        self.lbl_status.setText(status + (" (running)" if self._running else ""))

    def _pileup_sample(self, samples: list[int]) -> int | None:
        """First sample where the rise over the CFD delay exceeded the pile-up threshold after the
        pulse started to fall -- the host-side replay of capture_engine's test, used only to place
        the marker. None if the settings are unknown or the replay finds nothing (it may differ
        from the hardware by a sample or so of pipeline alignment)."""
        cfg = self._trigger_cfg
        if cfg is None or not getattr(cfg, "pileup_window", None) or cfg.cfd_delay is None:
            return None
        span = cfg.cfd_delay + 1
        sign = 1 if cfg.rising else -1
        fallen = False
        for n in range(max(cfg.delay, span), min(cfg.delay + cfg.pileup_window, len(samples))):
            rise = sign * (samples[n] - samples[n - span])
            if rise < 0:
                fallen = True
            elif fallen and rise > cfg.pileup_threshold:
                return n
        return None
