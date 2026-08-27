"""Live discrimination view: two independent scatter plots, each with its own controls beside it
-- FCI vs Energy on the left row, PSD vs Energy on the right row. Energy is energy_long (see
module-level rationale below). Fed by AcquisitionWorker.batch_received / stats_received.

There is only one underlying acquisition state ($AE/$AD pairs FCI and PSD together -- they cannot
be started independently), so the FCI and PSD Start/Stop button pairs are mirrored: clicking either
one's Start starts both sides' data flowing, and both button pairs are kept in sync rather than
implying an independence the protocol does not have.

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

from fci_api import AcqEvent, Stats

logger = logging.getLogger(__name__)


class _SideControls(QGroupBox):
    """One side's control-and-stats cluster (Start/Stop plus that side's own dropped/overflow
    counts). Two of these exist, one beside each plot -- see module docstring for why Start/Stop
    is mirrored between them rather than independent."""

    start_clicked = Signal()
    stop_clicked = Signal()

    def __init__(self, title: str, dropped_label: str, overflow_label: str):
        super().__init__(title)
        self._dropped_label = dropped_label
        self._overflow_label = overflow_label

        layout = QVBoxLayout(self)

        ops_layout = QHBoxLayout()
        self.btn_start = QPushButton("START")
        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_clicked.emit)
        self.btn_stop.clicked.connect(self.stop_clicked.emit)
        ops_layout.addWidget(self.btn_start)
        ops_layout.addWidget(self.btn_stop)
        layout.addLayout(ops_layout)

        stats_grid = QGridLayout()
        self.lbl_events = QLabel("0")
        self.lbl_excluded = QLabel("0")
        self.lbl_paired = QLabel("0")
        self.lbl_dropped = QLabel("0")
        self.lbl_overflow = QLabel("0")
        for row, (name, widget) in enumerate(
            [
                ("Events plotted:", self.lbl_events),
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

    def set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_start.setEnabled(enabled)
        if not enabled:
            self.btn_stop.setEnabled(False)

    def update_counts(self, events: int, excluded: int, paired: int, dropped: int,
                       overflow: int) -> None:
        self.lbl_events.setText(str(events))
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
        self._last_stats: Stats | None = None
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        fci_row = QHBoxLayout()
        self.fci_controls = _SideControls("FCI Acquisition", "Dropped (fci)", "Overflow (fci)")
        self.fci_controls.start_clicked.connect(self._on_start)
        self.fci_controls.stop_clicked.connect(self._on_stop)
        fci_row.addWidget(self.fci_controls, stretch=1)

        self.plot_fci = pg.PlotWidget(title="FCI vs Energy")
        self.plot_fci.setLabel("bottom", "Energy (energy_long, raw ADC-code·samples)")
        self.plot_fci.setLabel("left", "FCI")
        self.plot_fci.showGrid(x=True, y=True)
        self.scatter_fci = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(255, 200, 0, 140), pen=None)
        self.plot_fci.addItem(self.scatter_fci)
        fci_row.addWidget(self.plot_fci, stretch=3)
        layout.addLayout(fci_row)

        psd_row = QHBoxLayout()
        self.psd_controls = _SideControls("PSD Acquisition", "Dropped (psd)", "Overflow (psd)")
        self.psd_controls.start_clicked.connect(self._on_start)
        self.psd_controls.stop_clicked.connect(self._on_stop)
        psd_row.addWidget(self.psd_controls, stretch=1)

        self.plot_psd = pg.PlotWidget(title="PSD vs Energy")
        self.plot_psd.setLabel("bottom", "Energy (energy_long, raw ADC-code·samples)")
        self.plot_psd.setLabel("left", "PSD")
        self.plot_psd.showGrid(x=True, y=True)
        self.scatter_psd = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(100, 200, 255, 140), pen=None)
        self.plot_psd.addItem(self.scatter_psd)
        psd_row.addWidget(self.plot_psd, stretch=3)
        layout.addLayout(psd_row)

        # Energy is the shared x-axis for both plots -- panning/zooming one moves the other, so
        # the same energy slice is easy to compare across both discriminators.
        self.plot_psd.setXLink(self.plot_fci)

    def _on_start(self) -> None:
        self.fci_controls.set_running(True)
        self.psd_controls.set_running(True)
        self.start_clicked.emit()

    def _on_stop(self) -> None:
        self.fci_controls.set_running(False)
        self.psd_controls.set_running(False)
        self.stop_clicked.emit()

    def set_controls_enabled(self, enabled: bool) -> None:
        self.fci_controls.set_controls_enabled(enabled)
        self.psd_controls.set_controls_enabled(enabled)

    def clear(self) -> None:
        self._energy.clear()
        self._fci.clear()
        self._psd.clear()
        self._total_events = 0
        self._excluded_events = 0
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
        self._refresh_side_panels()

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
        self.fci_controls.update_counts(
            self._total_events, self._excluded_events, paired, dropped_fci, overflow_fci
        )
        self.psd_controls.update_counts(
            self._total_events, self._excluded_events, paired, dropped_psd, overflow_psd
        )
