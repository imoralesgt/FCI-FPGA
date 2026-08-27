"""Live discrimination view: two independent scatter plots, each flanked by that subsystem's own
controls -- configuration form on the LEFT of the plot, live statistics on the RIGHT. FCI vs Energy
on the top row, PSD vs Energy on the bottom row. Energy is energy_long (see module-level rationale
below). Fed by AcquisitionWorker.batch_received / stats_received.

There is only one underlying acquisition state ($AE/$AD pairs FCI and PSD together -- they cannot
be started independently), so the FCI and PSD Start/Stop/Reset button triples are mirrored:
clicking any one of them acts on both sides at once, rather than implying an independence the
protocol does not have.

Start/Stop only pause and resume the live event stream -- they do not touch the plotted data.
Clearing what's plotted is Reset's job alone, so a Stop followed by another Start (e.g. to briefly
freeze the display, or because the CSV segment should roll over) continues the same accumulated
view rather than silently discarding it.

Energy is energy_long (the PSD long-gate charge integral) -- the long gate is configured to cover
essentially the whole pulse (see project log section 8d and PsdConfig.long_gate's docstring), which
is what makes it a usable proxy for total deposited charge here. These are raw ADC-code-integrated
units, not a calibrated physical energy -- the axis is labelled accordingly rather than implying a
keV scale nothing here has established.

Events with energy_long <= 0 are excluded from both plots, not merely left to render as garbage:
that is the documented low-energy pathology (project log section 8d) where the BLR gate does not
close in time for a small pulse and the long-gate integral goes non-positive. Energy itself is not
meaningfully defined for such an event, and firmware's PSD ratio for it is a 0.0 sentinel for
"undefined" (see AcqEvent.psd's docstring), not a real measurement -- plotting either would
misrepresent a known-invalid result as data. The exclusion is counted and shown, not hidden.
"""

from __future__ import annotations

import logging
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fci_api import AcqEvent, FciClient, Stats

from .config_panel import FCI_FIELDS, PSD_FIELDS, SubsystemPanel

logger = logging.getLogger(__name__)

RATE_WINDOW_S = 3.0
"""Event rate is a sliding-window average over this many seconds, not a lifetime average -- a
lifetime average would stay skewed by however long acquisition sat paused, and would only decay
slowly after a real rate change. A short window tracks the current rate and naturally settles to
0 while paused, with no separate pause-awareness needed."""


class _ControlsPanel(QGroupBox):
    """Left-of-plot column: Start/Stop/Reset plus that subsystem's own configuration form. Two of
    these exist, one beside each plot -- see module docstring for why Start/Stop/Reset are
    mirrored between them rather than independent.
    """

    start_clicked = Signal()
    stop_clicked = Signal()
    reset_clicked = Signal()

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
        layout.addStretch(1)

    def set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_start.setEnabled(enabled)
        self.btn_reset.setEnabled(enabled)
        if not enabled:
            self.btn_stop.setEnabled(False)


class _StatsPanel(QGroupBox):
    """Right-of-plot column: live counts and the current event rate for that subsystem."""

    def __init__(self, title: str, dropped_label: str, overflow_label: str):
        super().__init__(title)
        self._rate_history: deque[tuple[float, float]] = deque()
        """(monotonic_time, rate_hz) pairs within RATE_HISTORY_WINDOW_S, oldest first -- feeds the
        small rate-vs-time plot below the counts. Independent of LiveView's own _rate_samples
        (that one is raw event timestamps used to COMPUTE the current rate; this one is a history
        of the already-computed rate values themselves, sampled once per update_counts() call)."""

        layout = QVBoxLayout(self)
        stats_grid = QGridLayout()
        self.lbl_events = QLabel("0")
        self.lbl_rate = QLabel("0.0")
        self.lbl_excluded = QLabel("0")
        self.lbl_paired = QLabel("0")
        self.lbl_dropped = QLabel("0")
        self.lbl_overflow = QLabel("0")
        for row, (name, widget) in enumerate(
            [
                ("Events plotted:", self.lbl_events),
                ("Event rate (Hz):", self.lbl_rate),
                ("Excluded (energy_long ≤ 0):", self.lbl_excluded),
                ("Paired:", self.lbl_paired),
                (f"{dropped_label}:", self.lbl_dropped),
                (f"{overflow_label}:", self.lbl_overflow),
            ]
        ):
            stats_grid.addWidget(QLabel(name), row, 0)
            stats_grid.addWidget(widget, row, 1)
        layout.addLayout(stats_grid)

        layout.addWidget(QLabel("Rate vs time:"))
        self.rate_plot = pg.PlotWidget()
        self.rate_plot.setMaximumHeight(80)
        self.rate_plot.showAxis("bottom", False)
        self.rate_plot.setLabel("left", "Hz")
        self.rate_plot.getPlotItem().setMenuEnabled(False)
        self.rate_plot.getPlotItem().hideButtons()
        self.rate_curve = self.rate_plot.plot(pen=pg.mkPen((0, 200, 120), width=1.5))
        layout.addWidget(self.rate_plot)

        layout.addStretch(1)

    RATE_HISTORY_WINDOW_S = 120.0
    """How far back the small rate-vs-time plot looks -- long enough to show a real trend, short
    enough that the plot stays readable at "very small" size without needing to decimate points."""

    def update_counts(self, events: int, rate_hz: float, excluded: int, paired: int,
                       dropped: int, overflow: int) -> None:
        self.lbl_events.setText(str(events))
        self.lbl_rate.setText(f"{rate_hz:.1f}")
        self.lbl_excluded.setText(str(excluded))
        self.lbl_paired.setText(str(paired))
        self.lbl_dropped.setText(str(dropped))
        self.lbl_overflow.setText(str(overflow))

        now = time.monotonic()
        self._rate_history.append((now, rate_hz))
        cutoff = now - self.RATE_HISTORY_WINDOW_S
        while self._rate_history and self._rate_history[0][0] < cutoff:
            self._rate_history.popleft()
        if self._rate_history:
            t0 = self._rate_history[0][0]
            xs = [t - t0 for t, _ in self._rate_history]
            ys = [r for _, r in self._rate_history]
            self.rate_curve.setData(xs, ys)

    def clear_rate_history(self) -> None:
        self._rate_history.clear()
        self.rate_curve.setData([], [])


class LiveView(QWidget):
    start_clicked = Signal()
    stop_clicked = Signal()
    fom_wizard_clicked = Signal()

    MAX_POINTS = 20_000
    """Sliding-window cap so a long session doesn't grow memory/render time without bound --
    mirrors the reference GUI's own PLOT_WINDOW_LEN pattern."""

    def __init__(self):
        super().__init__()
        self._energy: list[int] = []
        self._fci: list[float] = []
        self._psd: list[float] = []
        self._total_events = 0
        self._excluded_events = 0
        self._rate_samples: deque[tuple[float, int]] = deque()
        """(wall_clock_time, cumulative total_events) pairs within RATE_WINDOW_S, oldest first --
        see RATE_WINDOW_S's docstring for why this is a sliding window, not a lifetime average."""
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

        self.fci_config = SubsystemPanel("FCI Configuration", FCI_FIELDS, "get_fci", "set_fci")
        self.psd_config = SubsystemPanel("PSD Configuration", PSD_FIELDS, "get_psd", "set_psd")

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
        self.plot_fci.setLabel("bottom", "Energy (energy_long, raw ADC-code·samples)")
        self.plot_fci.setLabel("left", "FCI")
        self.plot_fci.showGrid(x=True, y=True)
        self.plot_fci.setMinimumHeight(self.PLOT_MIN_HEIGHT)
        # FCI/PSD are both normalized ratios, so 0-1 is a sensible starting window -- set once,
        # manually (autorange off), so it stays put rather than snapping to the data's own extent
        # on every batch. The user can still zoom/pan freely afterwards; this only fixes the
        # INITIAL view, not a permanent lock.
        self.plot_fci.enableAutoRange(axis="y", enable=False)
        self.plot_fci.setYRange(0, 1, padding=0)
        self.scatter_fci = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(255, 200, 0, 140), pen=None)
        self.plot_fci.addItem(self.scatter_fci)
        self.heatmap_fci = pg.ImageItem()
        self.heatmap_fci.setLookupTable(self._heatmap_lut())
        self.heatmap_fci.setVisible(False)
        self.plot_fci.addItem(self.heatmap_fci)
        grid.addWidget(self.plot_fci, 0, 1)

        self.fci_stats = _StatsPanel("FCI Statistics", "Dropped (fci)", "Overflow (fci)")
        grid.addWidget(self.fci_stats, 0, 2)

        self.psd_controls = _ControlsPanel("PSD Acquisition", self.psd_config)
        self.psd_controls.start_clicked.connect(self._on_start)
        self.psd_controls.stop_clicked.connect(self._on_stop)
        self.psd_controls.reset_clicked.connect(self._on_reset)
        grid.addWidget(self.psd_controls, 1, 0)

        self.plot_psd = pg.PlotWidget(title="PSD vs Energy")
        self.plot_psd.setLabel("bottom", "Energy (energy_long, raw ADC-code·samples)")
        self.plot_psd.setLabel("left", "PSD")
        self.plot_psd.showGrid(x=True, y=True)
        self.plot_psd.setMinimumHeight(self.PLOT_MIN_HEIGHT)
        self.plot_psd.enableAutoRange(axis="y", enable=False)
        self.plot_psd.setYRange(0, 1, padding=0)
        self.scatter_psd = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(100, 200, 255, 140), pen=None)
        self.plot_psd.addItem(self.scatter_psd)
        self.heatmap_psd = pg.ImageItem()
        self.heatmap_psd.setLookupTable(self._heatmap_lut())
        self.heatmap_psd.setVisible(False)
        self.plot_psd.addItem(self.heatmap_psd)
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

    HEATMAP_XBINS = 120
    HEATMAP_YBINS = 60
    """A 2D histogram over at most MAX_POINTS (20,000) events at this bin count is a few hundred
    microseconds to a couple of milliseconds with numpy -- cheap enough to recompute on every
    batch while heatmap view is active, same cadence as the scatter plot's own setData()."""

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
        if checked:
            self._update_heatmaps()
        else:
            self.scatter_fci.setData(self._energy, self._fci)
            self.scatter_psd.setData(self._energy, self._psd)

    def _update_heatmaps(self) -> None:
        if not self._energy:
            return
        energy = np.asarray(self._energy, dtype=np.float64)
        x_min, x_max = float(energy.min()), float(energy.max())
        if x_max <= x_min:
            x_max = x_min + 1.0
        for values, img in ((self._fci, self.heatmap_fci), (self._psd, self.heatmap_psd)):
            hist, _, _ = np.histogram2d(
                energy, np.asarray(values, dtype=np.float64),
                bins=(self.HEATMAP_XBINS, self.HEATMAP_YBINS),
                range=[[x_min, x_max], [0.0, 1.0]],
            )
            img.setImage(hist, autoLevels=True)
            img.setRect(QRectF(x_min, 0.0, x_max - x_min, 1.0))

    def get_accumulated_events(self) -> tuple[list[int], list[float], list[float]]:
        """(energy, fci, psd) parallel lists for whatever is currently plotted -- the FoM wizard's
        "live session data" source. Returns copies: the caller must not be able to corrupt this
        view's own buffers by mutating what it gets back."""
        return list(self._energy), list(self._fci), list(self._psd)

    def _on_start(self) -> None:
        if self.confirm_start is not None and not self.confirm_start():
            return
        self.fci_controls.set_running(True)
        self.psd_controls.set_running(True)
        self.start_clicked.emit()

    def _on_stop(self) -> None:
        self.fci_controls.set_running(False)
        self.psd_controls.set_running(False)
        self.stop_clicked.emit()

    def _on_reset(self) -> None:
        self.clear()

    def set_client(self, client: FciClient | None) -> None:
        self.fci_config.set_client(client)
        self.psd_config.set_client(client)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.fci_controls.set_controls_enabled(enabled)
        self.psd_controls.set_controls_enabled(enabled)

    def clear(self) -> None:
        self._energy.clear()
        self._fci.clear()
        self._psd.clear()
        self._total_events = 0
        self._excluded_events = 0
        self._rate_samples.clear()
        self._last_stats = None
        self.scatter_fci.setData([], [])
        self.scatter_psd.setData([], [])
        self.heatmap_fci.clear()
        self.heatmap_psd.clear()
        self.fci_stats.clear_rate_history()
        self.psd_stats.clear_rate_history()
        self._refresh_side_panels()

    def add_events(self, events: list[AcqEvent]) -> None:
        if not events:
            return
        for e in events:
            if e.energy_long <= 0:
                self._excluded_events += 1
                continue
            self._energy.append(e.energy_long)
            self._fci.append(e.fci)
            self._psd.append(e.psd)
            self._total_events += 1
        # _total_events is a true cumulative count, independent of the sliding-window trim below
        # -- it must NOT be derived from len(self._energy), or it would silently stop counting
        # (or even go backwards) once MAX_POINTS starts discarding the oldest plotted points.

        if len(self._energy) > self.MAX_POINTS:
            overflow = len(self._energy) - self.MAX_POINTS
            del self._energy[:overflow]
            del self._fci[:overflow]
            del self._psd[:overflow]

        if self.chk_heatmap.isChecked():
            self._update_heatmaps()
        else:
            self.scatter_fci.setData(self._energy, self._fci)
            self.scatter_psd.setData(self._energy, self._psd)

        self._rate_samples.append((time.monotonic(), self._total_events))
        self._refresh_side_panels()

    def _current_rate_hz(self) -> float:
        # Pruned here, against the live clock, rather than only when a new event arrives: the
        # periodic stats poll (update_stats(), roughly every couple seconds regardless of run
        # state) calls this too, which is what lets the displayed rate decay towards 0 once events
        # stop -- pruning only inside add_events() would leave a frozen, stale rate forever once
        # nothing new is coming in.
        now = time.monotonic()
        cutoff = now - RATE_WINDOW_S
        while len(self._rate_samples) > 1 and self._rate_samples[0][0] < cutoff:
            self._rate_samples.popleft()
        if not self._rate_samples or now - self._rate_samples[0][0] > RATE_WINDOW_S:
            return 0.0
        t0, n0 = self._rate_samples[0]
        t1, n1 = self._rate_samples[-1]
        dt = t1 - t0
        return (n1 - n0) / dt if dt > 0 else 0.0

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
        rate = self._current_rate_hz()
        self.fci_stats.update_counts(
            self._total_events, rate, self._excluded_events, paired, dropped_fci, overflow_fci
        )
        self.psd_stats.update_counts(
            self._total_events, rate, self._excluded_events, paired, dropped_psd, overflow_psd
        )
