"""Live energy spectrum tab: accumulates the FPGA-computed peak amplitude (fpga/rtl/psd_core's new
`peak` field -- see dual_gate_integrator.vhd) into a fixed-channel histogram, offers up to 3
calibration coefficients to relabel the x-axis in energy units, and exports to the ORTEC/Maestro
SPE ASCII format. The same coefficients are shared live with LiveView's FCI/PSD-vs-Energy plots
(see calibration_changed below), so this tab is where a session's one calibration is set.

Peak amplitude, not energy_long, is the spectroscopy channel here: it is a whole-pulse property
independent of the PSD gates and of the energy_long <= 0 BLR-gate pathology LiveView's FCI/PSD
plots have to exclude (see live_view.py's module docstring) -- every triggered pulse has a peak.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from fci_api import AcqEvent

HIST_CHANNELS = 16384
"""One bin per raw ADC code, 1:1, spanning the full 0..16383 theoretical range: 2^14, the ADC's
native resolution -- NOT the 16-bit width of the AXI-Stream datapath `peak` travels over
(dual_gate_integrator.vhd's DATA_WIDTH), which is wider than the sample data it actually carries.
Real events cluster in the lower part of that 16384 span -- the upper channels legitimately read
zero -- but the axis itself covers the whole theoretical ceiling, which is the normal convention
for this class of instrument rather than an axis auto-scaled to whatever was captured so far. This
is the accumulation resolution ONLY: DISPLAY_CHANNEL_CHOICES below lets the user view/export at a
coarser rebin without losing the underlying full-resolution counts (Clear is the only thing that
discards them)."""

DISPLAY_CHANNEL_CHOICES = [256, 512, 1024, 2048, 4096, 8192, 16384]
"""Selectable spectrum spans, via the slider. This detector's own energy resolution is ~6% at
Cs-137 (~40 keV FWHM at 662 keV) -- far coarser than one ADC code -- so 16384 raw channels is more
resolution than the physics can use and just makes every bin wait longer for the same statistical
significance. Each step is a clean power-of-2 divisor of HIST_CHANNELS (64x range end to end), so
rebinning is always an exact integer grouping with no remainder."""

RATE_WINDOW_S = 3.0
"""Instantaneous-rate sliding window -- same value and reasoning as live_view.py's own
RATE_WINDOW_S: long enough to smooth batch-to-batch noise, short enough to track a real rate
change and settle to 0 promptly once events stop."""

RATE_MIN_DT_S = 0.25
"""Below this much span between the oldest retained rate sample and now, report 0 rather than
count/dt -- avoids a divide-by-near-zero spike on the first sample after a gap. Same reasoning as
live_view.py's constant of the same name."""


class _SciDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that accepts scientific notation (e.g. 1.5e-05) as TYPED input.

    Qt's default QDoubleSpinBox validator accepts a complete scientific-notation string if it
    arrives all at once (paste, or setValue()), but rejects the INTERMEDIATE states typing produces
    one keystroke at a time -- "1.5e" and "1.5e-" both validate as flatly Invalid rather than
    Intermediate, which makes QAbstractSpinBox refuse the keystroke outright and a user can never
    actually type an exponent by hand. A QDoubleValidator explicitly in ScientificNotation mode
    accepts those same intermediate strings correctly (verified directly against both validators
    before writing this), so validate() delegates to one instead of the spin box's built-in one.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._validator = QDoubleValidator(self)
        self._validator.setNotation(QDoubleValidator.Notation.ScientificNotation)

    def setRange(self, minimum: float, maximum: float) -> None:  # noqa: N802 (Qt override)
        super().setRange(minimum, maximum)
        self._validator.setRange(minimum, maximum, self.decimals())

    def setDecimals(self, decimals: int) -> None:  # noqa: N802 (Qt override)
        super().setDecimals(decimals)
        self._validator.setRange(self.minimum(), self.maximum(), decimals)

    def validate(self, text: str, pos: int):
        return self._validator.validate(text, pos)

    def valueFromText(self, text: str) -> float:  # noqa: N802 (Qt override)
        try:
            return float(text)
        except ValueError:
            return self.value()

    def textFromValue(self, value: float) -> str:  # noqa: N802 (Qt override)
        return f"{value:.{self.decimals()}g}"


def _rebin_calibration(c0: float, c1: float, c2: float,
                        factor: int) -> tuple[float, float, float]:
    """Re-expresses calibration coefficients defined against the raw 0..HIST_CHANNELS-1 channel
    axis so they instead apply directly to a decimated channel index 0..(HIST_CHANNELS/factor - 1)
    -- i.e. to the SPE/plot convention where channel index runs over however many bins are actually
    present, not over the original raw resolution.

    Substitutes raw = factor*ch + m (m = the center-of-group offset, (factor-1)/2) into
    E = c0 + c1*raw + c2*raw^2 and collects terms back into E = c0' + c1'*ch + c2'*ch^2. At
    factor=1 (no decimation) this is the identity map, so callers do not need a separate code path
    for the undecimated case.
    """
    m = (factor - 1) / 2.0
    c0p = c0 + c1 * m + c2 * m * m
    c1p = c1 * factor + 2.0 * c2 * factor * m
    c2p = c2 * factor * factor
    return c0p, c1p, c2p


def _rebin_counts(counts: np.ndarray, display_channels: int) -> np.ndarray:
    factor = HIST_CHANNELS // display_channels
    if factor == 1:
        return counts
    return counts.reshape(display_channels, factor).sum(axis=1)


def write_spe(path: Path, counts: np.ndarray, calibration: tuple[float, float, float],
              live_time_s: float, real_time_s: float) -> None:
    """Writes an ORTEC/Maestro-style ASCII SPE file: $SPEC_ID, $DATE_MEA, $MEAS_TIM, $DATA and
    $MCA_CAL sections. live_time_s/real_time_s are equal here -- this instrument does not track
    dead time separately from wall-clock time, so live_time is reported as an approximation of it
    rather than omitted. `calibration` must already be expressed against `counts`'s own channel
    index (see _rebin_calibration) -- the SPE convention applies $MCA_CAL directly to $DATA's row
    position, not to some other, coarser-or-finer channel numbering. Always the RAW linear counts,
    regardless of the live view's log/lin display toggle -- that toggle is a display convenience,
    not a transform of the underlying data."""
    with open(path, "w", encoding="ascii") as f:
        f.write("$SPEC_ID:\nFCI-FPGA live spectrum\n")
        f.write("$DATE_MEA:\n" + time.strftime("%m/%d/%Y %H:%M:%S") + "\n")
        f.write(f"$MEAS_TIM:\n{live_time_s:.0f} {real_time_s:.0f}\n")
        f.write(f"$DATA:\n0 {len(counts) - 1}\n")
        for c in counts:
            f.write(f"{int(c)}\n")
        f.write("$MCA_CAL:\n3\n")
        f.write(" ".join(f"{c:.6E}" for c in calibration) + "\n")
        f.write("keVee\n")


class HistogramView(QWidget):
    calibration_changed = Signal(float, float, float)
    """Emitted with (c0, c1, c2) whenever any coefficient changes, so LiveView's FCI/PSD-vs-Energy
    plots (which compute their own keVee axis from the same coefficients applied to each event's
    peak) can stay in sync -- the same cross-tab live-wiring pattern main_window.py already uses
    for PSD pre_trigger / Trigger delay."""

    run_clicked = Signal()
    stop_clicked = Signal()
    """Tell the controller to start/stop the device-side amplitude poll ($RA) -- see
    AcquisitionWorker.request_spectrum_poll(). _running itself (gating add_events()) is purely
    local UI state and needs no controller involvement; these signals exist because ACTUALLY
    getting live data into add_events() while Live FCI/PSD is not running requires telling the
    reader process to start polling $RA, which this widget cannot do on its own -- it has no
    device connection of its own, by design (see set_controls_enabled())."""

    def __init__(self):
        super().__init__()
        self._counts = np.zeros(HIST_CHANNELS, dtype=np.int64)
        self._total = 0
        self._start_time: float | None = None
        self._running = True
        """Independent of the device connection and of Live FCI/PSD's own Start/Stop: this just
        gates whether add_events() accumulates incoming batches into the histogram. Defaults to
        running so behavior is unchanged for anyone not using the button."""
        self.bars: pg.BarGraphItem | None = None
        self._init_ui()
        self._redraw()
        self._reset_view_to_full_span()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Live energy spectrum from the FPGA-computed peak amplitude (list-mode: paired with "
            "FCI/PSD by the same event timestamp)."
        ))

        # One compact row rather than two stacked boxes: binning and calibration are both small,
        # occasional-adjustment controls, and the plot below is what this tab is actually for -- it
        # should get the vertical space, not a 3-row QFormLayout of calibration spinboxes.
        controls_box = QGroupBox("Binning and calibration")
        controls_layout = QHBoxLayout(controls_box)

        controls_layout.addWidget(QLabel("Bins:"))
        controls_layout.addWidget(QLabel("Coarser"))
        self.slider_channels = QSlider(Qt.Orientation.Horizontal)
        self.slider_channels.setRange(0, len(DISPLAY_CHANNEL_CHOICES) - 1)
        self.slider_channels.setValue(len(DISPLAY_CHANNEL_CHOICES) - 1)  # 16384, no decimation
        self.slider_channels.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider_channels.setTickInterval(1)
        self.slider_channels.setSingleStep(1)
        self.slider_channels.setPageStep(1)
        self.slider_channels.setMinimumWidth(260)
        self.slider_channels.valueChanged.connect(self._on_span_changed)
        controls_layout.addWidget(self.slider_channels)
        controls_layout.addWidget(QLabel("Finer"))
        self.lbl_channels = QLabel("")
        self.lbl_channels.setMinimumWidth(100)
        controls_layout.addWidget(self.lbl_channels)
        self._update_channels_label()

        self.chk_log_y = QCheckBox("Log scale")
        self.chk_log_y.setToolTip(
            "Y-axis in log scale: proper log-spaced ticks labeled with real count values, not a "
            "linear axis showing log10(counts). An empty bin reads as \"1\" rather than \"0\" -- "
            "the standard convention, since 0 has no position on a true log axis."
        )
        self.chk_log_y.toggled.connect(self._redraw)
        controls_layout.addWidget(self.chk_log_y)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        controls_layout.addWidget(divider)

        controls_layout.addWidget(QLabel("Energy Calib.:"))
        self.spin_c0 = _SciDoubleSpinBox()
        self.spin_c0.setRange(-1e9, 1e9)
        self.spin_c0.setDecimals(4)
        self.spin_c0.setSingleStep(0.1)
        self.spin_c0.setMaximumWidth(100)
        self.spin_c0.setToolTip("c0: offset (E = c0 + c1*channel + c2*channel^2, raw-channel "
                                 "basis). Accepts scientific notation, e.g. 1.5e-05.")
        self.spin_c1 = _SciDoubleSpinBox()
        self.spin_c1.setRange(-1e9, 1e9)
        self.spin_c1.setDecimals(6)
        self.spin_c1.setSingleStep(0.001)
        self.spin_c1.setValue(1.0)
        self.spin_c1.setMaximumWidth(100)
        self.spin_c1.setToolTip("c1: linear term. Accepts scientific notation, e.g. 1.5e-05.")
        self.spin_c2 = _SciDoubleSpinBox()
        self.spin_c2.setRange(-1e9, 1e9)
        self.spin_c2.setDecimals(9)
        self.spin_c2.setSingleStep(0.000001)
        self.spin_c2.setMaximumWidth(100)
        self.spin_c2.setToolTip("c2: quadratic term. Accepts scientific notation, e.g. 1.5e-09.")
        for label, spin in (("c0:", self.spin_c0), ("c1:", self.spin_c1), ("c2:", self.spin_c2)):
            controls_layout.addWidget(QLabel(label))
            controls_layout.addWidget(spin)
        for spin in (self.spin_c0, self.spin_c1, self.spin_c2):
            spin.valueChanged.connect(self._on_calibration_changed)

        controls_layout.addStretch(1)
        layout.addWidget(controls_box)

        ctrl_layout = QHBoxLayout()
        self.btn_run = QPushButton("Run")
        self.btn_run.setEnabled(False)  # already running by default
        self.btn_run.clicked.connect(self._on_run)
        ctrl_layout.addWidget(self.btn_run)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self._on_stop)
        ctrl_layout.addWidget(self.btn_stop)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear)
        ctrl_layout.addWidget(self.btn_clear)
        self.btn_export = QPushButton("Export to SPE...")
        self.btn_export.clicked.connect(self._export_spe)
        ctrl_layout.addWidget(self.btn_export)

        ctrl_divider = QFrame()
        ctrl_divider.setFrameShape(QFrame.Shape.VLine)
        ctrl_divider.setFrameShadow(QFrame.Shadow.Sunken)
        ctrl_layout.addWidget(ctrl_divider)

        self.lbl_status = QLabel("Total: 0 counts")
        ctrl_layout.addWidget(self.lbl_status)
        ctrl_layout.addWidget(QLabel("|"))
        self.lbl_rate = QLabel("Rate: 0.0 cps")
        self.lbl_rate.setToolTip(f"Instantaneous rate -- a {RATE_WINDOW_S:.0f} s sliding window, "
                                  "not a lifetime average. Decays to 0 shortly after events stop "
                                  "arriving (e.g. Stop, or a paused device).")
        ctrl_layout.addWidget(self.lbl_rate)
        ctrl_layout.addWidget(QLabel("|"))
        self.lbl_avg_rate = QLabel("Avg: 0.0 cps")
        self.lbl_avg_rate.setToolTip("Cumulative rate: total counts / elapsed time since the "
                                      "first event after the last Clear.")
        ctrl_layout.addWidget(self.lbl_avg_rate)

        ctrl_layout.addStretch(1)
        layout.addLayout(ctrl_layout)

        self._rate_samples: deque[tuple[float, int]] = deque()
        """(monotonic_time, batch_size) pairs within RATE_WINDOW_S, oldest first -- same sliding-
        window pattern as live_view.py's LiveView._rate_samples, for the same reason: a rate that
        never decays once the source stops is misleading, and a lifetime average masks the current
        rate behind however long the session has been open."""
        self._rate_timer = QTimer(self)
        self._rate_timer.setInterval(1000)
        self._rate_timer.timeout.connect(self._update_rate_labels)
        self._rate_timer.start()
        """Ticks independent of add_events(): the window must keep pruning (and the instantaneous
        rate keep decaying toward 0) even while nothing is arriving, which a purely
        event-driven update would never do once events stop."""

        self.plot_widget = pg.PlotWidget(title="Energy spectrum")
        self.plot_widget.setLabel("bottom", "Energy (keVee)")
        self.plot_widget.setLabel("left", "Counts")
        self.plot_widget.showGrid(x=True, y=True)
        layout.addWidget(self.plot_widget, stretch=1)

    def set_controls_enabled(self, enabled: bool) -> None:
        """Run/Stop, Clear and Export do not depend on a live device connection -- a spectrum
        already collected should stay controllable and exportable after disconnecting -- so
        nothing here is gated by it. Present for symmetry with the other tabs'
        set_controls_enabled(), called from MainWindow.set_connected_controls_enabled()."""

    def calibration(self) -> tuple[float, float, float]:
        return (self.spin_c0.value(), self.spin_c1.value(), self.spin_c2.value())

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------------------------- run/stop

    def _on_run(self) -> None:
        self._running = True
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._update_status_label()
        self.run_clicked.emit()

    def _on_stop(self) -> None:
        self._running = False
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._update_status_label()
        self.stop_clicked.emit()

    def _update_status_label(self) -> None:
        suffix = "" if self._running else "  (stopped)"
        self.lbl_status.setText(f"Total: {self._total} counts{suffix}")

    # ------------------------------------------------------------------------------------- data

    def add_events(self, events: list[AcqEvent]) -> None:
        if not self._running or not events:
            return
        if self._start_time is None:
            self._start_time = time.time()
        peaks = np.fromiter((e.peak for e in events), dtype=np.int64, count=len(events))
        # Clamped rather than dropped: a peak outside [0, HIST_CHANNELS) is a real, if unusual,
        # event (e.g. a triggered frame that never rose above baseline -- see PEAK_MIN's derivation
        # in dual_gate_integrator.vhd), and folding it into the nearest edge bin keeps every event
        # counted in _total, matching what the device's own event_count reports.
        np.clip(peaks, 0, HIST_CHANNELS - 1, out=peaks)
        self._counts += np.bincount(peaks, minlength=HIST_CHANNELS)
        self._total += len(events)
        self._rate_samples.append((time.monotonic(), len(events)))
        self._update_status_label()
        self._update_rate_labels()
        self._redraw()

    def clear(self) -> None:
        self._counts[:] = 0
        self._total = 0
        self._start_time = None
        self._rate_samples.clear()
        self._update_status_label()
        self._update_rate_labels()
        self._redraw()

    # ------------------------------------------------------------------------------------- rate

    def _instantaneous_rate_hz(self) -> float:
        now = time.monotonic()
        cutoff = now - RATE_WINDOW_S
        while self._rate_samples and self._rate_samples[0][0] < cutoff:
            self._rate_samples.popleft()
        if not self._rate_samples:
            return 0.0
        dt = now - self._rate_samples[0][0]
        if dt < RATE_MIN_DT_S:
            return 0.0
        return sum(n for _, n in self._rate_samples) / dt

    def _cumulative_rate_hz(self) -> float:
        if self._start_time is None:
            return 0.0
        elapsed = time.time() - self._start_time
        return (self._total / elapsed) if elapsed > 0 else 0.0

    def _update_rate_labels(self) -> None:
        self.lbl_rate.setText(f"Rate: {self._instantaneous_rate_hz():.1f} cps")
        self.lbl_avg_rate.setText(f"Avg: {self._cumulative_rate_hz():.1f} cps")

    # ------------------------------------------------------------------------------------- span

    def _display_channels(self) -> int:
        return DISPLAY_CHANNEL_CHOICES[self.slider_channels.value()]

    def _update_channels_label(self) -> None:
        n = self._display_channels()
        factor = HIST_CHANNELS // n
        self.lbl_channels.setText(f"{n} channels" + (f"  (x{factor})" if factor > 1 else ""))

    def _on_span_changed(self) -> None:
        self._update_channels_label()
        self._redraw()
        self._reset_view_to_full_span()

    def _on_calibration_changed(self) -> None:
        self._redraw()
        self._reset_view_to_full_span()
        self.calibration_changed.emit(*self.calibration())

    # ------------------------------------------------------------------------------------- plot

    def _display_calibration(self) -> tuple[float, float, float]:
        factor = HIST_CHANNELS // self._display_channels()
        return _rebin_calibration(self.spin_c0.value(), self.spin_c1.value(),
                                   self.spin_c2.value(), factor)

    def _redraw(self) -> None:
        if self.bars is not None:
            self.plot_widget.removeItem(self.bars)
            self.bars = None
        n = self._display_channels()
        counts = _rebin_counts(self._counts, n)
        c0, c1, c2 = self._display_calibration()
        idx = np.arange(n, dtype=np.float64)
        x = c0 + c1 * idx + c2 * idx * idx
        width = abs(x[1] - x[0]) if len(x) > 1 else 1.0
        log_y = self.chk_log_y.isChecked()
        heights = np.log10(counts.astype(np.float64) + 1.0) if log_y else counts
        # BarGraphItem does not respond to ViewBox.setLogMode() (it draws literal rectangles in
        # whatever coordinate space it is given, with no log-aware repaint logic), so the bars
        # themselves are pre-transformed to log10 space above rather than relying on that. The axis
        # is told separately -- AxisItem.setLogMode() -- to relabel its ticks as 10^x and to lay out
        # proper log-spaced minor ticks (1,2,3..9,10,20,30..) for whatever linear range it is
        # actually showing, matching data that is already pre-transformed rather than
        # double-transforming it. The one artifact: an empty bin sits at log10(0+1)=0, which this
        # labels "1" rather than "0" -- the standard convention for a log-scale spectrum, since 0
        # has no position on a true log axis at all.
        axis_left = self.plot_widget.getAxis("left")
        # Despite the name, AxisItem.setLogMode() does NOT stay confined to the axis: it also flips
        # the linked ViewBox's own logMode flag as a side effect (see AxisItem.setLogMode()'s
        # "inform the linked views of the change"). That flag only affects the ViewBox's *clamping*
        # of a future range, not the range already sitting in ViewBox.state['viewRange'] -- so right
        # after this toggles into log mode, that stale range is still whatever linear-scale value
        # was on screen a moment ago (a single saturated bin can hold tens of thousands of counts).
        # ViewBox.addItem()/removeItem() below only QUEUE a recompute (queueUpdateAutoRange()); they
        # don't apply one synchronously. If a repaint lands in that gap, AxisItem.updateAutoSIPrefix()
        # computes 10**np.array(self.range) against that still-linear range now that self.logMode is
        # True, which overflows float64 (RuntimeWarning: overflow encountered in power) since counts
        # in the tens of thousands are far past log-safe magnitude. Setting the axis's own .range
        # directly -- bypassing the ViewBox's deferred auto-range entirely -- closes that window
        # without disabling the ViewBox's continuous Y auto-range this live view otherwise relies on
        # (unlike setYRange(), which would turn that off for good).
        top = float(heights.max()) if heights.size else 1.0
        axis_left.setRange(0.0, max(top, 1.0))
        axis_left.setLogMode(log_y)
        self.plot_widget.setLabel("left", "Counts")
        # ALL bins, not just nonzero ones: BarGraphItem's own bounding box is what pyqtgraph's
        # "view all" / autoscale button fits to, and masking to nonzero bins would make that button
        # (and the initial view) fit to whatever happened to be populated instead of the full
        # theoretical span -- exactly the jumpy behavior _reset_view_to_full_span() exists to avoid.
        # A zero-height bar draws nothing visible, so this costs nothing but a wider bounding box.
        #
        # (0, 200, 120) is the same green live_view.py's rate curve uses -- reused here rather than
        # introducing a new shade, and picked over the blue this replaced because it reads clearly
        # against pyqtgraph's default grid/axis color, which the blue was too close to. pen matches
        # brush explicitly: BarGraphItem's default pen is a gray outline, which at thousands of
        # adjacent bins reads as a solid gray wash over the fill color rather than a border.
        self.bars = pg.BarGraphItem(x=x, height=heights, width=width * 0.9,
                                     brush=pg.mkBrush(0, 200, 120, 150),
                                     pen=pg.mkPen(0, 200, 120, 150))
        self.plot_widget.addItem(self.bars)

    def _reset_view_to_full_span(self) -> None:
        """Sets the x-view to the full theoretical span once (also switching that axis out of
        continuous autorange, the same side effect scope_view.py's fixed Y-range relies on), rather
        than on every redraw -- ordinary data arrival must not fight a zoom/pan the user is actively
        doing. Called when the axis's own definition changes (span slider, calibration) and once at
        startup; never from add_events()'s redraw path. pyqtgraph's own "view all" button (present
        by default on every PlotWidget) remains available to return here manually at any time, and
        because _redraw() always includes the full bin range in the BarGraphItem's bounds (see
        there), that button fits to the same full span this sets initially."""
        n = self._display_channels()
        c0, c1, c2 = self._display_calibration()
        x0 = c0
        x1 = c0 + c1 * (n - 1) + c2 * (n - 1) * (n - 1)
        lo, hi = (x0, x1) if x1 >= x0 else (x1, x0)
        if hi <= lo:
            hi = lo + 1.0
        self.plot_widget.setXRange(lo, hi, padding=0)

    # ------------------------------------------------------------------------------------- export

    def _export_spe(self) -> None:
        if self._total == 0:
            QMessageBox.information(self, "Nothing to Export", "No events accumulated yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Spectrum", "", "SPE files (*.spe)")
        if not path:
            return
        # Enforced here rather than trusted to the dialog's own filter: that behavior is native and
        # not consistent across platforms/window managers, and a user typing a bare name with no
        # extension (or a different one) should still get a valid, openable .spe file.
        out_path = Path(path)
        if out_path.suffix.lower() != ".spe":
            out_path = out_path.with_name(out_path.name + ".spe")
        elapsed = (time.time() - self._start_time) if self._start_time is not None else 0.0
        counts = _rebin_counts(self._counts, self._display_channels())
        try:
            write_spe(out_path, counts, self._display_calibration(),
                      live_time_s=elapsed, real_time_s=elapsed)
        except OSError as e:
            QMessageBox.warning(self, "Export Failed", str(e))
            return
        QMessageBox.information(self, "Export Complete", f"Wrote {out_path}")
