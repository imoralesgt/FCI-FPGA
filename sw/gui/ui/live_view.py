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

import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
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
        layout.addStretch(1)

    def update_counts(self, events: int, rate_hz: float, excluded: int, paired: int,
                       dropped: int, overflow: int) -> None:
        self.lbl_events.setText(str(events))
        self.lbl_rate.setText(f"{rate_hz:.1f}")
        self.lbl_excluded.setText(str(excluded))
        self.lbl_paired.setText(str(paired))
        self.lbl_dropped.setText(str(dropped))
        self.lbl_overflow.setText(str(overflow))


class LiveView(QWidget):
    start_clicked = Signal()
    stop_clicked = Signal()

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
        self.scatter_fci = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(255, 200, 0, 140), pen=None)
        self.plot_fci.addItem(self.scatter_fci)
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
        self.scatter_psd = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(100, 200, 255, 140), pen=None)
        self.plot_psd.addItem(self.scatter_psd)
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
