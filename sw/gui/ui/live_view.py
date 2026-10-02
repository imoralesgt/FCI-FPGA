"""Live discrimination view: two independent scatter plots, each flanked by that subsystem's own
controls -- configuration form on the LEFT of the plot, live statistics on the RIGHT. FCI vs Energy
on the top row, PSD vs Energy on the bottom row. Energy is keVee, computed from each event's FPGA
peak amplitude (AcqEvent.peak) through the SAME calibration coefficients the Spectrum tab's
HistogramView owns (see module-level rationale below) -- Fed by
AcquisitionWorker.batch_received / stats_received.

There is only one underlying acquisition state ($AE/$AD pairs FCI and PSD together -- they cannot
be started independently), so the FCI and PSD Start/Stop/Reset button triples are mirrored:
clicking any one of them acts on both sides at once, rather than implying an independence the
protocol does not have.

Start/Stop only pause and resume the live event stream -- they do not touch the plotted data.
Clearing what's plotted is Reset's job alone, so a Stop followed by another Start (e.g. to briefly
freeze the display, or because the CSV segment should roll over) continues the same accumulated
view rather than silently discarding it.

Energy: E = c0 + c1*ch + c2*ch^2, where `ch` is `peak` folded into HistogramView's channel units
(_energy_from_peak() below; histogram_view.DEFAULT_PEAK_FOLD). `peak` is pulse_shaper_core's shaped-pulse
plateau amplitude (a Jordanov-Knoll recursive trapezoidal filter -- see trapezoidal_filter.vhd) --
a whole-pulse property, independent of the PSD gates, unlike the energy_long this axis used before.
The fold matters because the coefficients are not this tab's own: they live in HistogramView
(set_calibration() below receives them via MainWindow's cross-tab wiring, the same pattern used for
PSD pre_trigger / Trigger delay) and are defined against ITS accumulation channel, so applying them
to a raw peak here puts this axis a factor of the fold off the Spectrum tab's. They default to the
identity map (c0=0, c1=1, c2=0), so an uncalibrated session still plots a sensible folded-peak axis
on the same scale as the Spectrum tab rather than a meaningless one. Because the
underlying stored value is `peak`, not a pre-computed energy, a calibration change retroactively
rescales every already-plotted point (see _recompute_energy()) rather than only affecting events
that arrive afterwards.

Events with energy_long <= 0 are STILL excluded from both plots, even though energy_long is no
longer the plotted axis: that is the documented low-energy pathology (project log section 8d) where
the BLR gate does not close in time for a small pulse and the long-gate integral goes non-positive,
and firmware's PSD ratio for such an event is a 0.0 sentinel for "undefined" (see AcqEvent.psd's
docstring), not a real measurement -- plotting it against ANY energy axis would misrepresent a
known-invalid PSD result as data. The exclusion is counted and shown, not hidden.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fci_api import AcqEvent, FciClient, Stats

from .config_panel import FCI_FIELDS, PSD_FIELDS, SubsystemPanel
from .histogram_view import DEFAULT_PEAK_FOLD
from .slider_spin import FractionField


def _energy_from_peak(peak, cal, fold):
    """E = c0 + c1*ch + c2*ch^2, where `ch` is the peak folded into HistogramView's channel units.

    The fold is not optional decoration: this tab does not own its calibration, it receives the
    Spectrum tab's (HistogramView.calibration_changed), and those coefficients are defined against a
    FOLDED accumulation channel, not a raw AcqEvent.peak -- see histogram_view.DEFAULT_PEAK_FOLD.
    Applying them to a raw peak here is what put the two tabs' energy axes a factor of 256 apart.
    `fold` tracks the device's `peaking` (set_peak_fold(), from HistogramView.peak_fold_changed),
    so it has to be passed in rather than read from a module constant.

    Scalar or ndarray `peak` both work, which is why all three call sites share this one function
    rather than each spelling the arithmetic out and only some of them getting the fold.
    """
    c0, c1, c2 = cal
    ch = peak / fold
    return c0 + c1 * ch + c2 * ch * ch

logger = logging.getLogger(__name__)

RATE_WINDOW_S = 3.0
"""Event rate is a sliding-window average over this many seconds, not a lifetime average -- a
lifetime average would stay skewed by however long acquisition sat paused, and would only decay
slowly after a real rate change. A short window tracks the current rate and naturally settles to
0 while paused, with no separate pause-awareness needed."""

RATE_MIN_DT_S = 0.25
"""Below this much elapsed span between the oldest retained sample and now, LiveView._rate_hz_for()
reports 0 rather than count/dt -- a bit more than one batch poll interval (config.py's 200 ms), so
the very first batch after any gap (fresh Start, or resuming after the window emptied during a
Stop) doesn't get divided by a near-zero dt and read as a spurious spike."""

FCI_DOT_RGB = (0xFC, 0xC3, 0x0A)   # #FCC30A -- FCI vs Energy points (and its LLD/ULD shading)
PSD_DOT_RGB = (0x8E, 0xCB, 0xF9)   # #8ECBF9 -- PSD vs Energy points (and its LLD/ULD shading)
DIVIDER_LINE_COLOR = (0, 200, 120)  # g/n divider line: the Spectrum histogram / Trigger trace green

DIVIDER_DEFAULT = 0.5
"""Where each g/n divider starts before a project supplies its own value. Mid-range of the [0, 1]
axis both discriminators are plotted on -- a neutral starting point, not a setting for any
particular detector (the PMT CLYC's DT values are FCI 0.47 / PSD 0.75, project log section 10.15)."""

DIVIDER_NOTE_DEBOUNCE_MS = 750
"""How long a divider must stay still before LiveView.dividers_changed fires. The line on the plot
and the rates follow the slider immediately; only the notification that makes the recorder write
a `# ... dividers changed:` line into the list file waits, so a drag leaves one note, not one per
slider step."""

ENERGY_REGION_FALLBACK = (0.0, 1.0)
"""Where a discriminator's LinearRegionItem starts if its LLD/ULD cut gets enabled before any
event has arrived yet (nothing real to anchor a range to). Replaced the moment there's data: see
LiveView._on_cut_enabled_toggled()."""


class _ControlsPanel(QGroupBox):
    """Left-of-plot column: Start/Stop/Reset, that subsystem's own configuration form, and a
    checkbox enabling that discriminator's LLD/ULD cut. Two of these exist, one beside each plot
    -- see module docstring for why Start/Stop/Reset are mirrored between them rather than
    independent; the cut, unlike those, is NOT mirrored -- FCI and PSD gate independently.

    The cut's actual range lives on the plot itself (LiveView's fci_energy_region/
    psd_energy_region, a pg.LinearRegionItem dragged directly on the energy axis). The two spin
    boxes beside the checkbox show the same LLD/ULD as numbers and edit it both ways: typing moves
    the region (bounds_edited), dragging the region updates the boxes (set_bounds()). They are
    editable only while the cut is enabled and nothing is being recorded. See
    LiveView._mask_for()."""

    start_clicked = Signal()
    stop_clicked = Signal()
    reset_clicked = Signal()
    cut_toggled = Signal(bool)
    bounds_edited = Signal(float, float)
    """(lld, uld) typed into the spin boxes, in keVee."""

    BOUND_RANGE_KEVEE = (-10_000.0, 1_000_000.0)
    """Wide enough never to clip a region dragged anywhere on a calibrated axis; the region, not
    the box, is what bounds the cut."""

    def __init__(self, title: str, config_panel: SubsystemPanel):
        super().__init__(title)
        self.config_panel = config_panel

        layout = QVBoxLayout(self)

        ops_layout = QHBoxLayout()
        self.btn_start = QPushButton("START")
        self.btn_stop = QPushButton("STOP")
        self.btn_reset = QPushButton("RESET")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_clicked.emit)
        self.btn_stop.clicked.connect(self.stop_clicked.emit)
        self.btn_reset.clicked.connect(self.reset_clicked.emit)
        ops_layout.addWidget(self.btn_start)
        ops_layout.addWidget(self.btn_stop)
        ops_layout.addWidget(self.btn_reset)
        layout.addLayout(ops_layout)

        layout.addWidget(config_panel)

        self.chk_cut_enabled = QCheckBox("LLD/ULD")
        self.chk_cut_enabled.setToolTip(
            "Gates this plot, its stats, and recording by an energy (keVee) range -- drag the "
            "shaded region's edges on the plot to set it."
        )
        self.chk_cut_enabled.toggled.connect(self.cut_toggled.emit)
        self.chk_cut_enabled.toggled.connect(self._update_bounds_enabled)

        self._cut_locked = False
        self.spin_lld = self._bound_spin("LLD (keVee)")
        self.spin_uld = self._bound_spin("ULD (keVee)")
        cut_row = QHBoxLayout()
        cut_row.addWidget(self.chk_cut_enabled)
        cut_row.addWidget(self.spin_lld, 1)
        cut_row.addWidget(self.spin_uld, 1)
        layout.addLayout(cut_row)
        self._update_bounds_enabled()

        layout.addStretch(1)

    def _bound_spin(self, tooltip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(1)
        spin.setRange(*self.BOUND_RANGE_KEVEE)
        spin.setSingleStep(10.0)
        # Commit on Enter / focus-out, not per keystroke: typing "6000" must not move the region
        # through 6, 60 and 600 on the way, each of which would be a cut change of its own.
        spin.setKeyboardTracking(False)
        spin.setToolTip(tooltip)
        spin.valueChanged.connect(
            lambda _: self.bounds_edited.emit(self.spin_lld.value(), self.spin_uld.value()))
        return spin

    def set_bounds(self, lld: float, uld: float) -> None:
        """Shows the region's current bounds, without echoing them back as an edit."""
        for spin, v in ((self.spin_lld, lld), (self.spin_uld, uld)):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)

    def set_cut_locked(self, locked: bool, tooltip: str) -> None:
        """Recording lock for the checkbox and both bounds (see LiveView.set_recording_lock())."""
        self._cut_locked = locked
        for w in (self.chk_cut_enabled, self.spin_lld, self.spin_uld):
            if locked and w.property("unlocked_tooltip") is None:
                w.setProperty("unlocked_tooltip", w.toolTip())
            w.setToolTip(tooltip if locked else (w.property("unlocked_tooltip") or ""))
        self.chk_cut_enabled.setEnabled(not locked)
        self._update_bounds_enabled()

    def _update_bounds_enabled(self) -> None:
        editable = self.chk_cut_enabled.isChecked() and not self._cut_locked
        self.spin_lld.setEnabled(editable)
        self.spin_uld.setEnabled(editable)

    def set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_start.setEnabled(enabled)
        self.btn_reset.setEnabled(enabled)
        if not enabled:
            self.btn_stop.setEnabled(False)


class _StatsPanel(QGroupBox):
    """Right-of-plot column: live counts, live time and rates for that subsystem, the per-class
    rates (class 0 = at or below the g/n divider, "gamma"; class 1 = above it, "neutron"), two
    small rate-vs-time plots (one per class), and the pairing-health counters folded away under
    a collapsible "Advanced statistics" section (closed by default -- they matter when diagnosing
    the link, not while taking data)."""

    GAMMA_PEN = "#EEF1F7"
    NEUTRON_PEN = "#ED692E"

    def __init__(self, title: str, dropped_label: str, overflow_label: str):
        super().__init__(title)
        self._rate_history: deque[tuple[float, float, float]] = deque()
        """(monotonic_time, gamma_rate_hz, neutron_rate_hz) within RATE_HISTORY_WINDOW_S, oldest
        first -- feeds the two rate-vs-time plots. A history of already-computed rates, sampled once
        per update_counts() call; independent of LiveView's _rate_samples, which are the raw
        per-batch values the rates are computed FROM."""

        layout = QVBoxLayout(self)
        stats_grid = QGridLayout()
        self.lbl_live_time = QLabel("0:00:00")
        self.lbl_live_time.setToolTip(
            "Time acquisition has been running since the last Reset (Start to Stop intervals "
            "summed; paused time excluded). Host wall-clock time -- not corrected for the "
            "device's own dead time, which it does not report.")
        counts_tip = ("Counts: cumulative since the last Reset, within this plot's LLD/ULD cut, each "
                      "event classified on arrival against the divider in force then -- the same "
                      "class the list file records. Rate: the last few seconds, recomputed against "
                      "the CURRENT divider and cut.")
        self.lbl_events = QLabel("0 | 0.00")
        self.lbl_gamma = QLabel("0 | 0.00")
        self.lbl_gamma.setToolTip("Class 0: at or below the g/n divider. " + counts_tip)
        self.lbl_neutron = QLabel("0 | 0.00")
        self.lbl_neutron.setToolTip("Class 1: above the g/n divider. " + counts_tip)
        self.lbl_events.setToolTip(counts_tip)
        for row, (name, widget) in enumerate(
            [
                ("Live time:", self.lbl_live_time),
                ("Total (counts) | Rate (Hz):", self.lbl_events),
                ("Gamma (counts) | Rate (Hz):", self.lbl_gamma),
                ("Neutron (counts) | Rate (Hz):", self.lbl_neutron),
            ]
        ):
            stats_grid.addWidget(QLabel(name), row, 0)
            stats_grid.addWidget(widget, row, 1)
        layout.addLayout(stats_grid)

        self.btn_advanced = QToolButton()
        self.btn_advanced.setText("Advanced statistics")
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setChecked(False)
        self.btn_advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_advanced.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_advanced.setAutoRaise(True)
        self.btn_advanced.toggled.connect(self._on_advanced_toggled)
        layout.addWidget(self.btn_advanced)

        self.advanced = QWidget()
        adv_grid = QGridLayout(self.advanced)
        adv_grid.setContentsMargins(12, 0, 0, 0)
        self.lbl_excluded = QLabel("0")
        self.lbl_paired = QLabel("0")
        self.lbl_dropped = QLabel("0")
        self.lbl_overflow = QLabel("0")
        for row, (name, widget) in enumerate(
            [
                ("Excluded (energy_long ≤ 0):", self.lbl_excluded),
                ("Paired:", self.lbl_paired),
                (f"{dropped_label}:", self.lbl_dropped),
                (f"{overflow_label}:", self.lbl_overflow),
            ]
        ):
            adv_grid.addWidget(QLabel(name), row, 0)
            adv_grid.addWidget(widget, row, 1)
        self.advanced.setVisible(False)
        layout.addWidget(self.advanced)

        self.gamma_plot, self.gamma_curve = self._rate_plot(layout, "Gamma rate vs time:",
                                                            self.GAMMA_PEN)
        self.neutron_plot, self.neutron_curve = self._rate_plot(layout, "Neutron rate vs time:",
                                                                self.NEUTRON_PEN)
        layout.addStretch(1)

    @staticmethod
    def _rate_plot(layout: QVBoxLayout, title: str, color) -> tuple[pg.PlotWidget, object]:
        layout.addWidget(QLabel(title))
        plot = pg.PlotWidget()
        plot.setMaximumHeight(70)
        plot.showAxis("bottom", False)
        plot.setLabel("left", "Hz")
        plot.getViewBox().setLimits(yMin=0)  # a rate is never negative; keeps an idle plot at 0
        # Three ways back from an accidental zoom on a plot this small: the "A" button (raised
        # above the left axis, which otherwise covers it in the bottom-left corner of a 70 px
        # plot), the right-click menu's "View All", and a double-click.
        plot.getPlotItem().autoBtn.setZValue(1000)
        vb = plot.getViewBox()
        plot.scene().sigMouseClicked.connect(
            lambda ev, vb=vb: vb.enableAutoRange() if ev.double() else None)
        curve = plot.plot(pen=pg.mkPen(color, width=1.5))
        layout.addWidget(plot)
        return plot, curve

    def _on_advanced_toggled(self, checked: bool) -> None:
        self.btn_advanced.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.advanced.setVisible(checked)

    RATE_HISTORY_WINDOW_S = 120.0
    """How far back the small rate-vs-time plots look -- long enough to show a real trend, short
    enough that the plots stay readable at "very small" size without needing to decimate points."""

    def update_counts(self, events: int, gammas: int, neutrons: int, live_time_s: float,
                      rate_hz: float, gamma_hz: float, neutron_hz: float, excluded: int,
                      paired: int, dropped: int, overflow: int) -> None:
        t = int(live_time_s)
        self.lbl_live_time.setText(f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}")
        self.lbl_events.setText(f"{events} | {rate_hz:.2f}")
        self.lbl_gamma.setText(f"{gammas} | {gamma_hz:.2f}")
        self.lbl_neutron.setText(f"{neutrons} | {neutron_hz:.2f}")
        self.lbl_excluded.setText(str(excluded))
        self.lbl_paired.setText(str(paired))
        self.lbl_dropped.setText(str(dropped))
        self.lbl_overflow.setText(str(overflow))

        now = time.monotonic()
        self._rate_history.append((now, gamma_hz, neutron_hz))
        cutoff = now - self.RATE_HISTORY_WINDOW_S
        while self._rate_history and self._rate_history[0][0] < cutoff:
            self._rate_history.popleft()
        if self._rate_history:
            t0 = self._rate_history[0][0]
            xs = [t - t0 for t, _, _ in self._rate_history]
            self.gamma_curve.setData(xs, [g for _, g, _ in self._rate_history])
            self.neutron_curve.setData(xs, [n for _, _, n in self._rate_history])

    def clear_rate_history(self) -> None:
        self._rate_history.clear()
        self.gamma_curve.setData([], [])
        self.neutron_curve.setData([], [])


class LiveView(QWidget):
    start_clicked = Signal()
    stop_clicked = Signal()
    fom_wizard_clicked = Signal()
    dividers_changed = Signal()
    """Either g/n divider settled on a new value (debounced, DIVIDER_NOTE_DEBOUNCE_MS). The recorder
    listens: every list-file row carries class_fci/class_psd computed against the dividers, so a
    change mid-recording is noted in the file -- see divider_settings_line()."""
    cuts_changed = Signal()
    """Either panel's LLD/ULD cut changed (enabled/disabled, or its region dragged). The recorder
    listens: filter_for_recording() decides which rows reach fci_live.csv by these cuts, so a
    change mid-recording changes what the file contains and has to be noted in it -- see
    cut_settings_line()."""

    MAX_POINTS = 1_000_000
    """Sliding-window cap on RETAINED points. Raised from 20,000: at the 10 kcps this instrument
    now sustains, 20,000 was a two-second window, so the view reset itself continuously and no
    structure could accumulate. A million points is ~100 s at 10 kcps, or hours at background
    rates, and costs ~24 MB across the three arrays.

    Affordable only because storage is numpy and drawing is decimated to MAX_PLOT_POINTS: the
    retained window is masked and strided per redraw, never converted or drawn in full.

    This caps only what is PLOTTED. Recording, the rate readout and the "Events captured" tally
    are unaffected."""

    REDRAW_HZ = 1
    """Plot repaint rate, fully decoupled from the arrival rate -- the acquisition thread polls as
    fast as the link allows while this ticks once a second.

    Was 10. Every redraw holds the GIL, and the reader needs it between reads; at 10 kcps the two
    were competing hard enough that throughput depended visibly on which plot type was displayed
    (a scatter redraw measured 3-5x a heatmap's, and the observed rate moved ~10k -> ~12k ev/s
    between them). At 1 Hz the redraw is a rounding error against acquisition, and a live scatter
    updating once a second is no worse to read -- the underlying distribution is not changing on
    a 100 ms timescale."""

    MAX_PLOT_POINTS = 50_000
    """Upper bound on points handed to setData() per redraw. Independent of MAX_POINTS, which
    bounds what is RETAINED; this bounds what is DRAWN.

    Was 4,000, chosen when redraws ran at 10 Hz and every millisecond of GIL time was starving the
    reader. At 1 Hz the budget is ten times larger: measured 44 ms for 50,000 points, i.e. 4.4%
    duty, against 17.4% for the same 20,000 points that used to cause byte loss at 10 Hz.

    It was also masking the retention increase. With the drawn count pinned at 4,000, raising
    retention from 20,000 to 1,000,000 changed nothing visible -- and worse, stride decimation
    re-samples a DIFFERENT subset each time the stride increments, so the cloud reshuffled between
    redraws and read as the plot resetting."""

    def __init__(self):
        super().__init__()
        self._cap = 1 << 16
        self._energy = np.empty(self._cap, dtype=np.float64)
        self._peak = np.empty(self._cap, dtype=np.float64)
        """Raw FPGA peak amplitude, kept alongside the derived _energy so a calibration change can
        rescale everything already plotted (see set_calibration()/_recompute_energy()) instead of
        only affecting events that arrive after the change."""
        self._fci = np.empty(self._cap, dtype=np.float64)
        self._psd = np.empty(self._cap, dtype=np.float64)
        self._n = 0
        """Numpy-backed rather than Python lists. With MAX_POINTS raised to hold a useful span at
        10 kcps, converting three lists to arrays on every redraw would cost tens of milliseconds
        on the GUI thread -- the exact cost that was starving the reader. Arrays grow by doubling
        and are compacted in place when the window overflows."""
        self._cal: tuple[float, float, float] = (0.0, 1.0, 0.0)
        """(c0, c1, c2) for E = c0 + c1*ch + c2*ch^2 against a folded channel (see
        _energy_from_peak()), pushed in from HistogramView via set_calibration(). Identity by
        default so an uncalibrated session plots folded-peak values, on the Spectrum tab's scale."""
        self._peak_fold = DEFAULT_PEAK_FOLD
        """Raw shaper counts per channel, from HistogramView.peak_fold_changed via
        set_peak_fold(). Must match what the calibration above was defined against."""
        self._total_events = 0
        self._excluded_events = 0
        self._fci_captured = 0
        self._psd_captured = 0
        """Cumulative events captured under each discriminator's cut -- a true running total, not
        a count of what is currently plotted. These are what the side panels show: the plotted
        count was capped by MAX_POINTS and so pinned at 20,000 while acquisition continued, which
        told the operator nothing about how much data they had actually collected. Counted at
        arrival under the cut that was active then, so these match what the CSV recorded; moving a
        region afterwards does not retroactively recount (it cannot -- events older than the
        plotting window are no longer held)."""
        self._fci_class1 = 0
        self._psd_class1 = 0
        """Of the captured events above, how many were class 1 (above that discriminator's divider)
        when they arrived; class 0 is captured - class 1. Same arrival-time rule as the list file's
        class_fci/class_psd columns, so the panel and the file agree."""
        self._rate_samples: deque[tuple[float, np.ndarray, np.ndarray, np.ndarray]] = deque()
        """(arrival_time, that batch's energies, FCI values, PSD values) -- one entry per BATCH, not
        per event. The values are kept so the class rates can be recomputed against the CURRENT
        dividers. See _rate_hz_for()."""
        self._dirty = False
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(int(1000 / self.REDRAW_HZ))
        self._redraw_timer.timeout.connect(self._on_redraw_tick)
        self._redraw_timer.start()
        """(monotonic_time, energy) once per event within RATE_WINDOW_S, oldest first (all
        events in the same batch share that batch's arrival time -- the granularity add_events()
        actually receives data at). Per-EVENT, not a running cumulative count like this used to
        be: the displayed rate is now AND-ed with each discriminator's own LLD/ULD cut
        (_rate_hz_for()), and a cut can change (drag, enable/disable) after events already
        arrived, which a plain endpoint-difference counter can't be filtered against
        retroactively -- recomputing the count from raw per-event records on every read can. See
        RATE_WINDOW_S's docstring for why this is a sliding window, not a lifetime average."""
        self._live_accum_s = 0.0
        self._run_started: float | None = None
        """Live time = sum of Start-to-Stop intervals since the last Reset: _live_accum_s holds the
        closed intervals, _run_started (monotonic) the open one, or None while stopped."""
        self._last_stats: Stats | None = None
        self.confirm_start = None
        """Optional callable, injected by the controller: () -> bool. Consulted before Start does
        anything -- lets the controller warn "recording will start" (and arm it) when the Record
        checkbox is on, and abort the whole click on Cancel, without this widget knowing anything
        about CSV logging itself."""
        self._init_ui()

    PLOT_MIN_HEIGHT = 260
    """Both plots get this same explicit floor so they end up the same size regardless of which
    side's config form happens to have more fields -- letting row height follow content alone
    would make the PSD row (one more field than FCI's) taller, and with it the PSD plot."""

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        top_row.addStretch(1)
        self.chk_heatmap = QCheckBox("Heatmap view")
        self.chk_heatmap.setToolTip(
            "Shows accumulation density (2D histogram) instead of individual points -- easier to "
            "read once enough events pile up that a scatter plot just looks like a solid blob."
        )
        self.chk_heatmap.toggled.connect(self._on_heatmap_toggled)
        top_row.addWidget(self.chk_heatmap)
        self.btn_fom_wizard = QPushButton("FoM Optimization...")
        self.btn_fom_wizard.clicked.connect(self.fom_wizard_clicked.emit)
        top_row.addWidget(self.btn_fom_wizard)
        layout.addLayout(top_row)

        divider_tip = ("Class divider for the {m} vs Energy plot, drawn as the horizontal line. "
                       "Events with {m} above it are class 1 (counted as neutrons), at or below it "
                       "class 0 (gammas); every list-mode row records the class as class_{k}. "
                       "Host-side only -- nothing is sent to the device, so it applies instantly "
                       "and works while disconnected.")
        self.fci_divider = FractionField(0.0, 1.0, 3, DIVIDER_DEFAULT)
        self.psd_divider = FractionField(0.0, 1.0, 3, DIVIDER_DEFAULT)
        self.fci_config = SubsystemPanel(
            "FCI Configuration", FCI_FIELDS, "get_fci", "set_fci",
            extra_rows=(("g/n divider", self.fci_divider, divider_tip.format(m="FCI", k="fci")),))
        self.psd_config = SubsystemPanel(
            "PSD Configuration", PSD_FIELDS, "get_psd", "set_psd",
            extra_rows=(("g/n divider", self.psd_divider, divider_tip.format(m="PSD", k="psd")),))
        self._divider_note_timer = QTimer(self)
        self._divider_note_timer.setSingleShot(True)
        self._divider_note_timer.setInterval(DIVIDER_NOTE_DEBOUNCE_MS)
        self._divider_note_timer.timeout.connect(self.dividers_changed.emit)

        # A single grid (not two independent QHBoxLayouts) so the config/plot/stats columns line
        # up at the same width on both rows -- two separate row layouts would each size their own
        # three widgets independently, and the FCI and PSD config forms don't have identical
        # natural widths, so the plots would end up different widths too.
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(2, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

        self.fci_controls = _ControlsPanel("FCI Acquisition", self.fci_config)
        self.fci_controls.start_clicked.connect(self._on_start)
        self.fci_controls.stop_clicked.connect(self._on_stop)
        self.fci_controls.reset_clicked.connect(self._on_reset)
        grid.addWidget(self.fci_controls, 0, 0)

        self.plot_fci = pg.PlotWidget(title="FCI vs Energy")
        self.plot_fci.setLabel("bottom", "Energy (keVee)")
        self.plot_fci.setLabel("left", "FCI")
        self.plot_fci.showGrid(x=True, y=True)
        self.plot_fci.setMinimumHeight(self.PLOT_MIN_HEIGHT)
        # FCI/PSD are both normalized ratios, so 0-1 is a sensible starting window -- set once,
        # manually (autorange off), so it stays put rather than snapping to the data's own extent
        # on every batch. The user can still zoom/pan freely afterwards; this only fixes the
        # INITIAL view, not a permanent lock.
        self.plot_fci.enableAutoRange(axis="y", enable=False)
        self.plot_fci.setYRange(0, 1, padding=0)
        self.scatter_fci = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(FCI_DOT_RGB + (140,)), pen=None)
        self.plot_fci.addItem(self.scatter_fci)
        self.heatmap_fci = pg.ImageItem()
        self.heatmap_fci.setLookupTable(self._heatmap_lut())
        self.heatmap_fci.setVisible(False)
        self.plot_fci.addItem(self.heatmap_fci)
        self.fci_energy_region = pg.LinearRegionItem(brush=pg.mkBrush(FCI_DOT_RGB + (40,)))
        self.fci_energy_region.setVisible(False)
        self.plot_fci.addItem(self.fci_energy_region)
        self.fci_controls.cut_toggled.connect(
            lambda checked: self._on_cut_enabled_toggled(self.fci_energy_region, checked)
        )
        self.fci_energy_region.sigRegionChangeFinished.connect(self._on_cut_changed)
        self._link_cut_bounds(self.fci_controls, self.fci_energy_region)
        self.fci_divider_line = self._divider_line(self.fci_divider.value())
        self.plot_fci.addItem(self.fci_divider_line)
        self.fci_divider.valueChanged.connect(
            lambda v: self._on_divider_changed(self.fci_divider_line, v))
        grid.addWidget(self.plot_fci, 0, 1)

        self.fci_stats = _StatsPanel("FCI Statistics", "Dropped (fci)", "Overflow (fci)")
        grid.addWidget(self.fci_stats, 0, 2)

        self.psd_controls = _ControlsPanel("PSD Acquisition", self.psd_config)
        self.psd_controls.start_clicked.connect(self._on_start)
        self.psd_controls.stop_clicked.connect(self._on_stop)
        self.psd_controls.reset_clicked.connect(self._on_reset)
        grid.addWidget(self.psd_controls, 1, 0)

        self.plot_psd = pg.PlotWidget(title="PSD vs Energy")
        self.plot_psd.setLabel("bottom", "Energy (keVee)")
        self.plot_psd.setLabel("left", "PSD")
        self.plot_psd.showGrid(x=True, y=True)
        self.plot_psd.setMinimumHeight(self.PLOT_MIN_HEIGHT)
        self.plot_psd.enableAutoRange(axis="y", enable=False)
        self.plot_psd.setYRange(0, 1, padding=0)
        self.scatter_psd = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(PSD_DOT_RGB + (140,)), pen=None)
        self.plot_psd.addItem(self.scatter_psd)
        self.heatmap_psd = pg.ImageItem()
        self.heatmap_psd.setLookupTable(self._heatmap_lut())
        self.heatmap_psd.setVisible(False)
        self.plot_psd.addItem(self.heatmap_psd)
        self.psd_energy_region = pg.LinearRegionItem(brush=pg.mkBrush(PSD_DOT_RGB + (40,)))
        self.psd_energy_region.setVisible(False)
        self.plot_psd.addItem(self.psd_energy_region)
        self.psd_controls.cut_toggled.connect(
            lambda checked: self._on_cut_enabled_toggled(self.psd_energy_region, checked)
        )
        self.psd_energy_region.sigRegionChangeFinished.connect(self._on_cut_changed)
        self._link_cut_bounds(self.psd_controls, self.psd_energy_region)
        self.psd_divider_line = self._divider_line(self.psd_divider.value())
        self.plot_psd.addItem(self.psd_divider_line)
        self.psd_divider.valueChanged.connect(
            lambda v: self._on_divider_changed(self.psd_divider_line, v))
        grid.addWidget(self.plot_psd, 1, 1)

        self.psd_stats = _StatsPanel("PSD Statistics", "Dropped (psd)", "Overflow (psd)")
        grid.addWidget(self.psd_stats, 1, 2)

        # A grid row's height is the max of its cells' own minimums, and the PSD config form has
        # one more field than FCI's -- so without this, row 1 would end up taller than row 0
        # regardless of the plots' matching setMinimumHeight() floors, since a row's surplus space
        # stacks on top of its own base rather than being split from a common zero. Equalizing the
        # two controls panels' own minimum height is what actually makes the two plots come out
        # the same size.
        equal_height = max(self.fci_controls.sizeHint().height(),
                            self.psd_controls.sizeHint().height())
        self.fci_controls.setMinimumHeight(equal_height)
        self.psd_controls.setMinimumHeight(equal_height)

        layout.addLayout(grid)

        # Energy is the shared x-axis for both plots -- panning/zooming one moves the other, so
        # the same energy slice is easy to compare across both discriminators.
        self.plot_psd.setXLink(self.plot_fci)

    @staticmethod
    def _divider_line(value: float) -> pg.InfiniteLine:
        """The g/n divider on a plot: horizontal, dashed, not draggable -- the slider is the one
        control that sets it, so the two cannot disagree. Drawn above the heatmap and scatter."""
        line = pg.InfiniteLine(pos=value, angle=0, movable=False,
                               pen=pg.mkPen(DIVIDER_LINE_COLOR, width=2, style=Qt.PenStyle.DashLine))
        line.setZValue(10)
        return line

    def _on_divider_changed(self, line: pg.InfiniteLine, value: float) -> None:
        line.setValue(value)
        self._refresh_side_panels()
        self._divider_note_timer.start()  # restarts on every step: fires once the slider rests

    def set_recording_lock(self, locked: bool, tooltip: str) -> None:
        """While recording: no LLD/ULD enable/disable, no dragging the cut regions, and no moving
        the g/n dividers -- the first two decide which rows are written, the dividers decide their
        class_fci/class_psd, and one recording should carry one setting of each. (Apply is locked
        by MainWindow on every SubsystemPanel.)"""
        for div in (self.fci_divider, self.psd_divider):
            if locked and div.property("unlocked_tooltip") is None:
                div.setProperty("unlocked_tooltip", div.toolTip())
            div.setEnabled(not locked)
            div.setToolTip(tooltip if locked else (div.property("unlocked_tooltip") or ""))
        for controls, region in ((self.fci_controls, self.fci_energy_region),
                                 (self.psd_controls, self.psd_energy_region)):
            controls.set_cut_locked(locked, tooltip)
            region.setMovable(not locked)

    def dividers(self) -> tuple[float, float]:
        """(fci_divider, psd_divider), as shown (3 decimals)."""
        return self.fci_divider.value(), self.psd_divider.value()

    def divider_settings_line(self) -> str:
        """One line for the list-mode CSV header (and for the notes written when a divider changes
        mid-recording): the values class_fci / class_psd were computed against."""
        f, p = self.dividers()
        return (f"dividers: fci={f:.3f}, psd={p:.3f}  "
                "[class_x = 1 if x > divider else 0; 1 = above the line (neutron-like), "
                "0 = at or below (gamma-like)]")

    def _cut_widgets(self):
        return (("fci_cut", self.fci_controls, self.fci_energy_region),
                ("psd_cut", self.psd_controls, self.psd_energy_region))

    def project_settings(self) -> dict:
        """Live-view host state a project keeps: the two dividers and both LLD/ULD cuts (not device
        registers, so not in the SubsystemPanels' own get_values()). Each cut is stored as
        {"enabled", "lld", "uld"}, bounds in keVee on the calibration in force when saved; the
        bounds are kept even when the cut is disabled."""
        f, p = self.dividers()
        out: dict = {"fci_divider": f, "psd_divider": p}
        for key, controls, region in self._cut_widgets():
            lo, hi = region.getRegion()
            out[key] = {"enabled": controls.chk_cut_enabled.isChecked(),
                        "lld": round(float(lo), 3), "uld": round(float(hi), 3)}
        return out

    def apply_project_settings(self, settings: dict) -> None:
        for key, field in (("fci_divider", self.fci_divider), ("psd_divider", self.psd_divider)):
            v = settings.get(key)
            if isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0:
                field.setValue(float(v))
        for key, controls, region in self._cut_widgets():
            cut = settings.get(key)
            if not isinstance(cut, dict):
                continue  # a project saved before cuts were stored: leave the cut as it is
            lo, hi = cut.get("lld"), cut.get("uld")
            bounds_ok = (isinstance(lo, (int, float)) and isinstance(hi, (int, float))
                         and math.isfinite(lo) and math.isfinite(hi) and lo < hi)
            # Checkbox first: enabling it resets the region to the data span
            # (_on_cut_enabled_toggled()), so the stored bounds have to be applied after it.
            controls.chk_cut_enabled.setChecked(bool(cut.get("enabled", False)))
            if bounds_ok:
                region.setRegion((float(lo), float(hi)))

    HEATMAP_XBINS = 512
    HEATMAP_YBINS = 512
    """Raised from 120x60, which could not resolve the features this instrument exists to measure.

    At YBINS=60 over the fixed 0..1 discriminant axis a bin is 0.0167 wide. The paper's gamma peak
    has an FCI FWHM of 0.0093 -- HALF a bin -- and the PSD Li-6 capture band is ~0.0015, a tenth of
    one. Any structure that narrow was being flattened into a single row before it could be seen.
    512 bins puts ~19 bins across a 0.0015 FWHM instead of a tenth of one.

    Cost is dominated by the point count, not the bin count -- histogram2d over the full retained
    window is ~83 ms at 1,000,000 points either way, i.e. ~8% duty at the 1 Hz redraw."""

    @staticmethod
    def _heatmap_lut() -> np.ndarray:
        return pg.colormap.get("viridis").getLookupTable(0.0, 1.0, 256)

    def _on_heatmap_toggled(self, checked: bool) -> None:
        self.scatter_fci.setVisible(not checked)
        self.scatter_psd.setVisible(not checked)
        self.heatmap_fci.setVisible(checked)
        self.heatmap_psd.setVisible(checked)
        # Whichever representation was just hidden stops being updated in add_events() (see
        # there), so it can be stale by however much accumulated while the other one was active --
        # refresh it now, on the switch, rather than leaving it stale until the next batch happens
        # to arrive.
        self._refresh_plots()

    @staticmethod
    def _link_cut_bounds(controls: _ControlsPanel, region: pg.LinearRegionItem) -> None:
        """Keeps a panel's LLD/ULD boxes and its plot region in step. Region -> boxes follows the
        drag live (sigRegionChanged); boxes -> region goes through setRegion(), which emits the
        region's own change-finished signal, so a typed bound takes the same _on_cut_changed()
        path as a drag. setRegion() sorts nothing, so an LLD typed above the ULD is ordered here;
        the boxes then show the ordered pair."""
        region.sigRegionChanged.connect(lambda r: controls.set_bounds(*r.getRegion()))
        controls.bounds_edited.connect(
            lambda lo, hi: region.setRegion((min(lo, hi), max(lo, hi))))
        controls.set_bounds(*region.getRegion())

    def _on_cut_enabled_toggled(self, region: pg.LinearRegionItem, checked: bool) -> None:
        region.setVisible(checked)
        if checked:
            # Reset to "everything currently accumulated" every time the cut is (re-)enabled,
            # rather than remembering wherever it was last dragged to -- a fresh, predictable
            # starting point (narrow FROM here) beats resuming a stale range from a previous,
            # possibly very different, session.
            if self._n:
                view = self._energy[: self._n]
                lo, hi = float(view.min()), float(view.max())
                if hi <= lo:
                    hi = lo + 1.0
            else:
                lo, hi = ENERGY_REGION_FALLBACK
            region.setRegion((lo, hi))
        self._on_cut_changed()

    def _on_cut_changed(self) -> None:
        """A panel's own LLD/ULD cut changed (enabled/disabled, or its region was dragged) --
        both the plot (_refresh_plots) and that panel's readout
        (_refresh_side_panels) depend on it, so both need redoing; neither add_events() nor
        update_stats() would otherwise run again until the next batch."""
        self._refresh_plots()
        self._refresh_side_panels()
        self.cuts_changed.emit()

    def cut_settings_line(self) -> str:
        """One line describing both LLD/ULD cuts, in keVee, for the list-mode CSV header (and for
        the notes written when a cut changes mid-recording). Host state, not a device register,
        but it decides which events are in the file at all (filter_for_recording()), so a dataset
        whose cuts are not recorded cannot tell a detector's real spectral edge from an operator's
        cut -- the 43.7 h PMT CLYC run's hard stop at 4.2 MeVee (project log section 10) was
        exactly that, and had to be reconstructed from memory. Bounds are in the calibration
        in force when the line is written, i.e. the header's own `energy:` line."""
        parts = []
        for name, controls, region in (("fci", self.fci_controls, self.fci_energy_region),
                                       ("psd", self.psd_controls, self.psd_energy_region)):
            if controls.chk_cut_enabled.isChecked():
                lo, hi = region.getRegion()
                parts.append(f"{name}_lld={lo:.1f}, {name}_uld={hi:.1f}")
            else:
                parts.append(f"{name}=off")
        return ("cuts: " + ", ".join(parts)
                + "  [keVee; a row is recorded only if it passes every enabled cut]")

    @staticmethod
    def _cut_keeps(controls: "_ControlsPanel", region: pg.LinearRegionItem,
                   energy: float) -> bool:
        """Scalar form of _mask_for, for tallying one event as it arrives."""
        if not controls.chk_cut_enabled.isChecked():
            return True
        lo, hi = region.getRegion()
        return bool(lo <= energy <= hi)

    @staticmethod
    def _mask_for(controls: "_ControlsPanel", region: pg.LinearRegionItem,
                  energy: np.ndarray) -> np.ndarray:
        """True for every energy sample this discriminator's cut would keep. An unchecked cut
        keeps everything -- the region only constrains anything once its checkbox is on."""
        if not controls.chk_cut_enabled.isChecked():
            return np.ones(len(energy), dtype=bool)
        lo, hi = region.getRegion()
        return (energy >= lo) & (energy <= hi)

    def filter_for_recording(self, events: list[AcqEvent]) -> list[AcqEvent]:
        """Events kept for the CSV log: since fci_live.csv is one row per event carrying BOTH
        discriminators' values, an event survives only if it passes EVERY currently-enabled cut
        (an unchecked cut imposes no constraint) -- the same AND-of-enabled-cuts a reader would
        expect from "this row's FCI value is in-range AND its PSD value is in-range". Does not
        re-apply the energy_long <= 0 exclusion; that's unconditional already (see add_events())
        and unrelated to this cut. The energy each event is compared against is computed fresh
        from e.peak and the CURRENT calibration, matching what the region's own bounds mean now
        that the plot's axis is keVee rather than energy_long -- see the module docstring."""
        out = []
        for e in events:
            energy = _energy_from_peak(e.peak, self._cal, self._peak_fold)
            if self.fci_controls.chk_cut_enabled.isChecked():
                lo, hi = self.fci_energy_region.getRegion()
                if not (lo <= energy <= hi):
                    continue
            if self.psd_controls.chk_cut_enabled.isChecked():
                lo, hi = self.psd_energy_region.getRegion()
                if not (lo <= energy <= hi):
                    continue
            out.append(e)
        return out

    def _refresh_plots(self) -> None:
        """Applies each discriminator's own LLD/ULD cut to the shared accumulated arrays and
        redraws whichever representation -- scatter or heatmap -- is currently visible. FCI and
        PSD are masked independently, so the two can show different energy slices of the same
        underlying event stream. Called after every new batch, on the heatmap/scatter toggle, and
        whenever either panel's cut changes (enabled/disabled or dragged)."""
        energy = self._energy[: self._n]
        heatmap = self.chk_heatmap.isChecked()
        for values, controls, region, scatter, img in (
            (self._fci, self.fci_controls, self.fci_energy_region, self.scatter_fci,
             self.heatmap_fci),
            (self._psd, self.psd_controls, self.psd_energy_region, self.scatter_psd,
             self.heatmap_psd),
        ):
            mask = self._mask_for(controls, region, energy)
            e = energy[mask]
            v = values[: self._n][mask]
            if heatmap:
                self._update_heatmap(e, v, img)
            else:
                # Decimated. This runs on the GUI thread and holds the GIL for its whole duration,
                # which starves the acquisition thread of scheduling -- and at 4 Mbaud the host
                # serial buffer overflows in tens of milliseconds, silently dropping bytes. That
                # was not a cosmetic problem: a dropped byte misaligns the binary $RQ frame onto a
                # payload byte, and since almost every field's high byte is zero the reader lands
                # on 0x00 and rejects the frame. Redraw cost was corrupting acquisition.
                #
                # Plotting the whole retained window is not worth it: a million points against a
                # few hundred thousand device pixels is almost entirely invisible overdraw.
                if len(e) > self.MAX_PLOT_POINTS:
                    # Take the most RECENT MAX_PLOT_POINTS rather than striding the whole window.
                    # Striding samples a different subset whenever the stride changes, so points
                    # visibly jump between redraws; a tail slice is stable -- a point, once drawn,
                    # stays put until it ages out. The older part of the retained window is still
                    # there for the heatmap, the FoM wizard and recording; it is only the scatter
                    # that shows a bounded recent slice, which is what a scatter is legible for.
                    e = e[-self.MAX_PLOT_POINTS:]
                    v = v[-self.MAX_PLOT_POINTS:]
                scatter.setData(e, v)

    def _update_heatmap(self, energy: np.ndarray, values: np.ndarray, img: pg.ImageItem) -> None:
        if len(energy) == 0:
            img.clear()
            return
        x_min, x_max = float(energy.min()), float(energy.max())
        if x_max <= x_min:
            x_max = x_min + 1.0
        # Fixed [0, 1], NOT auto-ranged to the data's own min/max: FCI and PSD are both normalized
        # ratios with that theoretical range (the scatter plots already fix their Y axis the same
        # way, setYRange(0, 1, padding=0)). An auto-ranged axis was tried first, on the reasoning
        # that both discriminators occupy a narrow slice of [0,1] (PSD typically ~0.96..1.00) and a
        # fixed axis would waste bins on empty space -- but that let a single pathological point
        # (e.g. PSD briefly negative from short > long on a noisy pulse, which is not otherwise
        # excluded by anything add_events() does) stretch the axis far past where the real
        # population sits, compressing it into a handful of rows: unreadable for the opposite
        # reason. Fixed bounds do lose some resolution on a tightly-clustered run, but never break.
        y_min, y_max = 0.0, 1.0
        hist, _, _ = np.histogram2d(
            energy, values, bins=(self.HEATMAP_XBINS, self.HEATMAP_YBINS),
            range=[[x_min, x_max], [y_min, y_max]],
        )
        # log1p (not log): most bins are near-empty and a few are dense, so a linear color
        # scale saturates the busy bins and makes everything else invisible; log1p(0) = 0
        # keeps empty bins mapped cleanly instead of producing -inf.
        img.setImage(np.log1p(hist), autoLevels=True)
        img.setRect(QRectF(x_min, y_min, x_max - x_min, y_max - y_min))

    def get_accumulated_events(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(energy, fci, psd) parallel numpy arrays for whatever is currently plotted -- the FoM wizard's
        "live session data" source. Returns copies: the caller must not be able to corrupt this
        view's own buffers by mutating what it gets back."""
        # Copies, so the caller cannot corrupt this view's buffers -- and sliced to _n, since the
        # backing arrays are over-allocated and the tail beyond _n is uninitialised memory.
        return (self._energy[: self._n].copy(),
                self._fci[: self._n].copy(),
                self._psd[: self._n].copy())

    def _live_time_s(self) -> float:
        if self._run_started is None:
            return self._live_accum_s
        return self._live_accum_s + (time.monotonic() - self._run_started)

    def _mark_stopped(self) -> None:
        if self._run_started is not None:
            self._live_accum_s += time.monotonic() - self._run_started
            self._run_started = None

    def _on_start(self) -> None:
        if self.confirm_start is not None and not self.confirm_start():
            return
        if self._run_started is None:
            self._run_started = time.monotonic()
        self.fci_controls.set_running(True)
        self.psd_controls.set_running(True)
        self.start_clicked.emit()

    def _on_stop(self) -> None:
        self._mark_stopped()
        self.fci_controls.set_running(False)
        self.psd_controls.set_running(False)
        self.stop_clicked.emit()

    def _on_reset(self) -> None:
        self.clear()

    def set_client(self, client: FciClient | None) -> None:
        self.fci_config.set_client(client)
        self.psd_config.set_client(client)

    def set_controls_enabled(self, enabled: bool) -> None:
        if not enabled:
            self._mark_stopped()  # a dropped connection ends the run as far as live time goes
        self.fci_controls.set_controls_enabled(enabled)
        self.psd_controls.set_controls_enabled(enabled)

    def clear(self) -> None:
        self._n = 0
        self._total_events = 0
        self._excluded_events = 0
        self._fci_captured = 0
        self._psd_captured = 0
        self._fci_class1 = 0
        self._psd_class1 = 0
        self._rate_samples.clear()
        self._live_accum_s = 0.0
        if self._run_started is not None:
            self._run_started = time.monotonic()  # Reset while running restarts the count from now
        self._last_stats = None
        self.scatter_fci.setData([], [])
        self.scatter_psd.setData([], [])
        self.heatmap_fci.clear()
        self.heatmap_psd.clear()
        self.fci_stats.clear_rate_history()
        self.psd_stats.clear_rate_history()
        self._refresh_side_panels()

    def _grow(self, needed: int) -> None:
        """Doubles capacity until `needed` fits. Amortised O(1) per event."""
        if needed <= self._cap:
            return
        while self._cap < needed:
            self._cap *= 2
        for name in ("_energy", "_peak", "_fci", "_psd"):
            old = getattr(self, name)
            new = np.empty(self._cap, dtype=np.float64)
            new[: self._n] = old[: self._n]
            setattr(self, name, new)

    def set_calibration(self, c0: float, c1: float, c2: float) -> None:
        """Receives (c0, c1, c2) from HistogramView.calibration_changed. Rescales every
        already-plotted point (see _recompute_energy()) rather than only affecting events that
        arrive afterwards -- the stored ground truth is each event's raw `peak`, not a pre-baked
        energy, precisely so a mid-session calibration change can do this."""
        self._cal = (c0, c1, c2)
        self._recompute_energy()
        self._refresh_plots()
        self._refresh_side_panels()

    def set_peak_fold(self, fold: int) -> None:
        """Receives the fold from HistogramView.peak_fold_changed (the device's `peaking`). Same
        retroactive rescale as set_calibration(), and for the same reason: `peak` is what is
        stored, so changing what a channel means re-derives every point already plotted. Unlike
        HistogramView, nothing is discarded here -- this view's stored ground truth is unaffected
        by the fold, only its derived energy axis is."""
        if fold <= 0 or fold == self._peak_fold:
            return
        self._peak_fold = fold
        self._recompute_energy()
        self._refresh_plots()
        self._refresh_side_panels()

    def _recompute_energy(self) -> None:
        self._energy[: self._n] = _energy_from_peak(self._peak[: self._n], self._cal, self._peak_fold)

    @staticmethod
    def _cut_mask(controls: "_ControlsPanel", region: pg.LinearRegionItem,
                  energy: np.ndarray) -> np.ndarray:
        """Vector form of _cut_keeps, for tallying a whole batch at once."""
        if not controls.chk_cut_enabled.isChecked():
            return np.ones(len(energy), dtype=bool)
        lo, hi = region.getRegion()
        return (energy >= lo) & (energy <= hi)

    def _on_redraw_tick(self) -> None:
        """Repaints only if new events arrived since the last tick, so an idle instrument costs
        nothing and a busy one costs a fixed REDRAW_HZ rather than one redraw per batch."""
        if not self._dirty:
            return
        self._dirty = False
        self._refresh_plots()
        self._refresh_side_panels()

    def add_events(self, events: list[AcqEvent]) -> None:
        if not events:
            return
        now = time.monotonic()
        # Vectorised: at 10 kcps a batch carries up to 1024 events, and a per-event Python loop
        # here runs on the GUI thread. _total_events stays a true cumulative count, independent of
        # the sliding-window trim below -- it must NOT be derived from the array length, or it
        # would stop counting (or go backwards) once MAX_POINTS starts discarding the oldest.
        # energy_long still decides which events are valid at all (see module docstring), even
        # though it is no longer what gets plotted -- a 0.0 PSD sentinel is still not a real
        # measurement no matter what the x-axis represents.
        el_arr = np.fromiter((e.energy_long for e in events), dtype=np.float64, count=len(events))
        keep = el_arr > 0
        self._excluded_events += int((~keep).sum())
        k = int(keep.sum())
        if k == 0:
            return
        peak_arr = np.fromiter((e.peak for e in events), dtype=np.float64, count=len(events))[keep]
        f_arr = np.fromiter((e.fci for e in events), dtype=np.float64, count=len(events))[keep]
        p_arr = np.fromiter((e.psd for e in events), dtype=np.float64, count=len(events))[keep]
        self._total_events += k

        energy_arr = _energy_from_peak(peak_arr, self._cal, self._peak_fold)

        self._grow(self._n + k)
        self._energy[self._n:self._n + k] = energy_arr
        self._peak[self._n:self._n + k] = peak_arr
        self._fci[self._n:self._n + k] = f_arr
        self._psd[self._n:self._n + k] = p_arr
        self._n += k

        self._rate_samples.append((now, energy_arr, f_arr, p_arr))

        # Tallied at arrival under whichever cut is active now -- see the counters' own docstring
        # for why this is not derived from the plotted arrays.
        fci_div, psd_div = self.dividers()
        fci_in = self._cut_mask(self.fci_controls, self.fci_energy_region, energy_arr)
        psd_in = self._cut_mask(self.psd_controls, self.psd_energy_region, energy_arr)
        self._fci_captured += int(fci_in.sum())
        self._psd_captured += int(psd_in.sum())
        self._fci_class1 += int(np.count_nonzero(fci_in & (f_arr > fci_div)))
        self._psd_class1 += int(np.count_nonzero(psd_in & (p_arr > psd_div)))

        if self._n > self.MAX_POINTS:
            drop = self._n - self.MAX_POINTS
            for a in (self._energy, self._peak, self._fci, self._psd):
                a[: self.MAX_POINTS] = a[drop : drop + self.MAX_POINTS]
            self._n = self.MAX_POINTS

        # Mark dirty and let the repaint timer coalesce, rather than redrawing here. Redrawing per
        # batch was costing acquisition throughput, not just frames: a full setData() over up to
        # MAX_POINTS points runs on the GUI thread and holds the GIL, which starves the worker
        # thread doing the serial round trips. Once adaptive polling raised the batch rate to ~30/s
        # that became ~30 full scatter rebuilds per second, and the effect was directly observable
        # -- the live event rate DROPPED when the window was focused and Qt actually repainted.
        # Coalescing to REDRAW_HZ decouples render cost from event rate entirely.
        self._dirty = True

    def _rate_hz_for(self, controls: "_ControlsPanel", region: pg.LinearRegionItem,
                     column: int, divider: float) -> tuple[float, float, float]:
        """(total, class 0, class 1) rates of events passing this discriminator's CURRENT LLD/ULD
        cut (or all events, if its cut is disabled) -- the same AND-with-the-cut treatment as the
        plot, "Events captured", and recording. `column` picks this discriminator's values in each
        _rate_samples entry (2 = FCI, 3 = PSD); class 1 is value > divider. Both the cut and the
        divider are applied to the retained per-batch arrays at read time, so moving either
        updates the rates immediately rather than only for events arriving afterwards.

        Pruned against the live clock, not only when a new event arrives: the periodic stats poll
        (update_stats(), roughly every couple seconds regardless of run state) calls this too,
        which is what lets the displayed rates decay towards 0 once events stop."""
        now = time.monotonic()
        cutoff = now - RATE_WINDOW_S
        while len(self._rate_samples) > 1 and self._rate_samples[0][0] < cutoff:
            self._rate_samples.popleft()
        if not self._rate_samples or now - self._rate_samples[0][0] > RATE_WINDOW_S:
            return 0.0, 0.0, 0.0
        dt = now - self._rate_samples[0][0]
        if dt < RATE_MIN_DT_S:
            # The first batch after any gap has every retained sample within one poll interval of
            # "now" -- dividing by a near-zero dt would report a spurious spike; read it as "not
            # enough history yet", same as the empty-window case above.
            return 0.0, 0.0, 0.0
        cut_on = controls.chk_cut_enabled.isChecked()
        lo, hi = region.getRegion() if cut_on else (0.0, 0.0)
        total = above = 0
        for entry in self._rate_samples:
            e, v = entry[1], entry[column]
            if cut_on:
                m = (e >= lo) & (e <= hi)
                v = v[m]
            total += len(v)
            above += int(np.count_nonzero(v > divider))
        return total / dt, (total - above) / dt, above / dt

    def update_stats(self, stats: Stats) -> None:
        self._last_stats = stats
        self._refresh_side_panels()

    def _refresh_side_panels(self) -> None:
        s = self._last_stats
        paired = s.paired if s else 0
        dropped_fci = s.dropped_fci if s else 0
        dropped_psd = s.dropped_psd if s else 0
        overflow_fci = s.overflow_fci if s else 0
        overflow_psd = s.overflow_psd if s else 0
        # "Events captured" is the cumulative tally kept by add_events(); it is deliberately NOT
        # recomputed from self._energy here. Doing that was the old behavior and it capped at
        # MAX_POINTS, so the figure froze at 20,000 while acquisition carried on. Both it and the
        # rate still honour each discriminator's OWN LLD/ULD cut rather than a shared total, so
        # each panel reflects that plot's slice of the recorded stream.
        live = self._live_time_s()
        fci_div, psd_div = self.dividers()
        f_tot, f_g, f_n = self._rate_hz_for(self.fci_controls, self.fci_energy_region, 2, fci_div)
        p_tot, p_g, p_n = self._rate_hz_for(self.psd_controls, self.psd_energy_region, 3, psd_div)
        self.fci_stats.update_counts(
            self._fci_captured, self._fci_captured - self._fci_class1, self._fci_class1, live,
            f_tot, f_g, f_n, self._excluded_events, paired, dropped_fci, overflow_fci)
        self.psd_stats.update_counts(
            self._psd_captured, self._psd_captured - self._psd_class1, self._psd_class1, live,
            p_tot, p_g, p_n, self._excluded_events, paired, dropped_psd, overflow_psd)
