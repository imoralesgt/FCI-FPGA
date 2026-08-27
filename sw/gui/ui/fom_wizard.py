"""Figure-of-merit (FoM) wizard for PSD and/or FCI, with two independent tabs:

  Optimize (live sweep): runs fom_sweep_worker.FomSweepWorker against the connected device --
    sweeps selected discrimination parameters one at a time, measuring FoM at each grid point on
    fresh live events, and applies whichever value scored best. Can only use live hardware -- see
    that module's docstring for why a static dataset cannot answer "what would the FoM have been
    with a different parameter value".

  Compute FoM (fixed dataset): a single, instant double-Gaussian fit (fom_core.compute_fom) over
    either this session's already-accumulated live events or an external CSV file. No device
    access, no sweeping -- for checking separation in data you already have, not for improving it.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fci_api import FciClient
from fom_core import (
    FCI_SWEEP_PARAMS,
    PSD_SWEEP_PARAMS,
    FomFitError,
    FomResult,
    SweepParam,
    compute_fom,
)
from fom_sweep_worker import DiscriminatorSweepPlan, FomSweepWorker, SweepPlan

logger = logging.getLogger(__name__)

LLD_RANGE = (0.0, 1_000_000_000.0)
"""Both LLD and ULD's range -- the Energy axis (energy_long) shared with the FCI/PSD-vs-Energy
plots. This project has no calibrated physical energy, and different sessions/files can span very
different scales, so this stays fixed and wide rather than trying to bound it in advance."""


class _EnergyCutFields(QWidget):
    """LLD + optional ULD -- the only energy-region selection this wizard offers, shared by both
    tabs' per-discriminator panels."""

    def __init__(self):
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)

        self.spin_lld = QDoubleSpinBox()
        self.spin_lld.setRange(*LLD_RANGE)
        self.spin_lld.setDecimals(2)
        self.spin_lld.setToolTip("Events with energy below this are excluded.")
        form.addRow("LLD (energy):", self.spin_lld)

        uld_row = QHBoxLayout()
        self.chk_uld = QCheckBox("ULD:")
        self.chk_uld.setToolTip("Optional -- also excludes events with energy above this.")
        self.spin_uld = QDoubleSpinBox()
        self.spin_uld.setRange(*LLD_RANGE)
        self.spin_uld.setDecimals(2)
        self.spin_uld.setValue(LLD_RANGE[1])
        self.spin_uld.setEnabled(False)
        self.chk_uld.toggled.connect(self.spin_uld.setEnabled)
        uld_row.addWidget(self.chk_uld)
        uld_row.addWidget(self.spin_uld)
        form.addRow(uld_row)

    def uld(self) -> float | None:
        return self.spin_uld.value() if self.chk_uld.isChecked() else None

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.spin_lld.setEnabled(enabled)
        self.chk_uld.setEnabled(enabled)
        self.spin_uld.setEnabled(enabled and self.chk_uld.isChecked())


class _ResultPanel(QWidget):
    """Histogram + fit curve + FoM readout for one discriminator's result. Shared by both tabs:
    the Optimize tab updates this live, once per grid point, as "the plots that already exist"."""

    def __init__(self, title: str, color: tuple[int, int, int]):
        super().__init__()
        layout = QVBoxLayout(self)
        self.plot = pg.PlotWidget(title=f"{title} histogram")
        self.plot.setLabel("bottom", title)
        self.plot.setLabel("left", "Counts")
        self.bars: pg.BarGraphItem | None = None
        self.curve = self.plot.plot(pen=pg.mkPen(color, width=2))
        layout.addWidget(self.plot)
        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        layout.addWidget(self.lbl_result)
        self._color = color

    def show_result(self, r: FomResult) -> None:
        if self.bars is not None:
            self.plot.removeItem(self.bars)
        width = (r.bin_centers[1] - r.bin_centers[0]) if len(r.bin_centers) > 1 else 1.0
        self.bars = pg.BarGraphItem(x=r.bin_centers, height=r.counts, width=width * 0.9,
                                     brush=pg.mkBrush(*self._color, 120))
        self.plot.addItem(self.bars)
        self.curve.setData(r.bin_centers, r.fit_curve)
        self.lbl_result.setText(
            f"n = {r.n_events} events   FoM = {r.fom:.3f}\n"
            f"peak 1: centroid={r.mu1:.4g}  FWHM={r.fwhm1:.4g}\n"
            f"peak 2: centroid={r.mu2:.4g}  FWHM={r.fwhm2:.4g}\n"
            f"separation S = {r.separation:.4g}"
        )

    def show_error(self, message: str) -> None:
        if self.bars is not None:
            self.plot.removeItem(self.bars)
            self.bars = None
        self.curve.setData([], [])
        self.lbl_result.setText(f"Could not compute FoM: {message}")


# ------------------------------------------------------------------------------------- Optimize tab


class _OptimizeDiscriminatorPanel(QGroupBox):
    def __init__(self, title: str, sweep_params, set_fn_name: str, value_field: str,
                 color: tuple[int, int, int]):
        super().__init__(title)
        self.set_fn_name = set_fn_name
        self.value_field = value_field
        self._sweep_params = sweep_params

        self.chk_enabled = QCheckBox(f"Optimize {title}")
        self.chk_enabled.setChecked(True)
        self.energy_cut = _EnergyCutFields()

        self.param_checks: dict[str, QCheckBox] = {}
        self.param_min_spins: dict[str, QSpinBox] = {}
        self.param_max_spins: dict[str, QSpinBox] = {}
        params_box = QGroupBox("Parameters to sweep")
        params_layout = QGridLayout(params_box)
        params_layout.addWidget(QLabel("Sweep"), 0, 0)
        params_layout.addWidget(QLabel("Parameter"), 0, 1)
        params_layout.addWidget(QLabel("Min"), 0, 2)
        params_layout.addWidget(QLabel("Max"), 0, 3)
        for row, p in enumerate(sweep_params, start=1):
            cb = QCheckBox()
            self.param_checks[p.name] = cb
            params_layout.addWidget(cb, row, 0)
            params_layout.addWidget(QLabel(p.label), row, 1)

            spin_min = QSpinBox()
            spin_min.setRange(p.minimum, p.maximum)
            spin_min.setValue(p.minimum)
            spin_min.setToolTip(f"Device valid range: [{p.minimum}, {p.maximum}]")
            self.param_min_spins[p.name] = spin_min
            params_layout.addWidget(spin_min, row, 2)

            spin_max = QSpinBox()
            spin_max.setRange(p.minimum, p.maximum)
            spin_max.setValue(p.maximum)
            spin_max.setToolTip(f"Device valid range: [{p.minimum}, {p.maximum}]")
            self.param_max_spins[p.name] = spin_max
            params_layout.addWidget(spin_max, row, 3)
        self._params_box = params_box

        self.result = _ResultPanel(title, color)

        self.lbl_status = QLabel("Idle")
        self.lbl_status.setStyleSheet("font-weight: bold;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m grid points")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMaximumHeight(140)

        layout = QVBoxLayout(self)
        layout.addWidget(self.chk_enabled)
        layout.addWidget(self.energy_cut)
        layout.addWidget(params_box)
        layout.addWidget(self.lbl_status)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.result)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self.log)

        self.chk_enabled.toggled.connect(self.energy_cut.setEnabled)
        self.chk_enabled.toggled.connect(params_box.setEnabled)

    def selected_params(self) -> list[SweepParam]:
        """Uses the user's own min/max spinboxes, not the field's full declared range -- clamped
        to that range by the spinboxes themselves, but otherwise entirely the user's choice."""
        out = []
        for p in self._sweep_params:
            if not self.param_checks[p.name].isChecked():
                continue
            lo = self.param_min_spins[p.name].value()
            hi = self.param_max_spins[p.name].value()
            out.append(SweepParam(p.name, p.label, lo, hi))
        return out

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def set_progress(self, completed: int, total: int) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(completed)

    def reset_progress(self) -> None:
        """Indeterminate ('busy') rather than a static 0 -- collecting events for the first grid
        point can take a while with nothing to report yet, and a motionless bar during that stretch
        reads as frozen rather than working. set_progress() above switches it to a real N/M count
        the moment the first grid point actually completes."""
        self.progress_bar.setRange(0, 0)

    def build_plan(self) -> DiscriminatorSweepPlan:
        return DiscriminatorSweepPlan(
            enabled=self.chk_enabled.isChecked(),
            lld=self.energy_cut.spin_lld.value(),
            uld=self.energy_cut.uld(),
            params=self.selected_params(),
            set_fn_name=self.set_fn_name,
            value_field=self.value_field,
        )

    def set_running(self, running: bool) -> None:
        self.chk_enabled.setEnabled(not running)
        self.energy_cut.setEnabled(not running and self.chk_enabled.isChecked())
        self._params_box.setEnabled(not running and self.chk_enabled.isChecked())
        if not running:
            self.lbl_status.setText("Idle")
        elif self.chk_enabled.isChecked():
            self.lbl_status.setText("⏳ Running...")
        else:
            self.lbl_status.setText("Not included in this run")
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)  # clears any leftover N/M from a previous run


# --------------------------------------------------------------------------------- Compute FoM tab


class _ComputeDiscriminatorPanel(QGroupBox):
    def __init__(self, title: str):
        super().__init__(title)
        self.chk_enabled = QCheckBox(f"Compute {title}")
        self.chk_enabled.setChecked(True)
        self.energy_cut = _EnergyCutFields()
        layout = QVBoxLayout(self)
        layout.addWidget(self.chk_enabled)
        layout.addWidget(self.energy_cut)
        self.chk_enabled.toggled.connect(self.energy_cut.setEnabled)


def _load_csv(path: Path) -> tuple[list[float], list[float], list[float]]:
    """Reads energy_long/fci/psd columns from a CSV matching CsvLogger's schema -- `#`-prefixed
    comment lines (its header block) are skipped, same convention used throughout this project."""
    energy, fci, psd = [], [], []
    with open(path, newline="", encoding="utf-8") as f:
        rows = (line for line in f if not line.startswith("#"))
        reader = csv.DictReader(rows)
        if reader.fieldnames is None or not {"energy_long", "fci", "psd"} <= set(reader.fieldnames):
            raise ValueError(
                "CSV must have 'energy_long', 'fci', and 'psd' columns (the GUI's own live-log "
                "schema, or a file converted to match it)."
            )
        for row in reader:
            try:
                energy.append(float(row["energy_long"]))
                fci.append(float(row["fci"]))
                psd.append(float(row["psd"]))
            except ValueError:
                continue  # a malformed/partial row -- skip rather than fail the whole load
    if not energy:
        raise ValueError("no usable rows found in that file")
    return energy, fci, psd


# --------------------------------------------------------------------------------------- FomWizard


class FomWizard(QDialog):
    def __init__(self, client: FciClient | None, acquisition_worker, get_live_events, parent=None):
        """`client`/`acquisition_worker` are None when not connected -- the Optimize tab disables
        itself in that case, but Compute FoM still works (it can analyze a file, or whatever this
        session already accumulated before disconnecting). `get_live_events` is a zero-arg
        callable returning (energy, fci, psd) parallel lists -- injected rather than importing
        LiveView here, to keep this dialog's only dependency on the rest of the GUI narrow."""
        super().__init__(parent)
        self._client = client
        self._acq_worker = acquisition_worker
        self._get_live_events = get_live_events
        self._sweep_worker: FomSweepWorker | None = None

        self._energy: np.ndarray | None = None
        self._fci: np.ndarray | None = None
        self._psd: np.ndarray | None = None

        self.setWindowTitle("FoM Optimization")
        self.setModal(True)
        self.resize(1150, 900)

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        self._build_optimize_tab()
        self._build_compute_tab()

    # ------------------------------------------------------------------------------- Optimize tab

    def _build_optimize_tab(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(QLabel(
            "Sweeps device parameters live, one at a time, measuring the FoM at each step on "
            "fresh events and keeping whichever value scored best."
        ))

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Steps per parameter:"))
        self.spin_steps = QSpinBox()
        self.spin_steps.setRange(3, 50)
        self.spin_steps.setValue(8)
        ctrl_row.addWidget(self.spin_steps)
        ctrl_row.addWidget(QLabel("Events per grid point:"))
        self.spin_events = QSpinBox()
        self.spin_events.setRange(20, 20000)
        self.spin_events.setValue(300)
        ctrl_row.addWidget(self.spin_events)
        ctrl_row.addStretch(1)
        self.btn_start_opt = QPushButton("Start Optimization")
        self.btn_start_opt.clicked.connect(self._start_optimization)
        self.btn_stop_opt = QPushButton("Stop")
        self.btn_stop_opt.setEnabled(False)
        self.btn_stop_opt.clicked.connect(self._stop_optimization)
        ctrl_row.addWidget(self.btn_start_opt)
        ctrl_row.addWidget(self.btn_stop_opt)
        layout.addLayout(ctrl_row)

        if self._client is None:
            layout.addWidget(QLabel(
                "Not connected -- Optimize needs a live device connection (it cannot run against "
                "previously recorded or already-accumulated data; see the Compute FoM tab for that)."
            ))
            self.btn_start_opt.setEnabled(False)

        panels_row = QHBoxLayout()
        self.psd_opt = _OptimizeDiscriminatorPanel("PSD", PSD_SWEEP_PARAMS, "set_psd", "psd",
                                                     (255, 140, 0))
        self.fci_opt = _OptimizeDiscriminatorPanel("FCI", FCI_SWEEP_PARAMS, "set_fci", "fci",
                                                     (0, 170, 255))
        panels_row.addWidget(self.psd_opt)
        panels_row.addWidget(self.fci_opt)
        layout.addLayout(panels_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, "Live FoM optimization")

    def _start_optimization(self) -> None:
        if self._client is None:
            return
        if not self.psd_opt.chk_enabled.isChecked() and not self.fci_opt.chk_enabled.isChecked():
            QMessageBox.information(self, "Nothing to Do", "Enable PSD, FCI, or both.")
            return

        plans: dict[str, DiscriminatorSweepPlan] = {}
        for key, panel in (("psd", self.psd_opt), ("fci", self.fci_opt)):
            if not panel.chk_enabled.isChecked():
                continue
            p = panel.build_plan()
            if not p.params:
                QMessageBox.warning(self, "Nothing Selected",
                                     f"Enable at least one parameter to sweep for {key.upper()}.")
                return
            bad = [sp.label for sp in p.params if sp.minimum >= sp.maximum]
            if bad:
                QMessageBox.warning(self, "Invalid Range",
                                     f"{key.upper()}: Min must be less than Max for: "
                                     f"{', '.join(bad)}.")
                return
            plans[key] = p
            panel.log.clear()
            panel.reset_progress()
        if not plans:
            return

        plan = SweepPlan(steps=self.spin_steps.value(), events_per_point=self.spin_events.value(),
                          discriminators=plans)
        self._sweep_worker = FomSweepWorker(self._client, self._acq_worker, plan)
        self._sweep_worker.log_line.connect(self._on_log_line)
        self._sweep_worker.grid_result.connect(self._on_grid_result)
        self._sweep_worker.progress.connect(self._on_progress)
        self._sweep_worker.finished_all.connect(self._on_sweep_finished)
        self._sweep_worker.error.connect(self._on_sweep_error)

        self.btn_start_opt.setEnabled(False)
        self.btn_stop_opt.setEnabled(True)
        self.psd_opt.set_running(True)
        self.fci_opt.set_running(True)
        self._sweep_worker.start()

    def _stop_optimization(self) -> None:
        if self._sweep_worker is not None:
            self._sweep_worker.request_stop()
            self.btn_stop_opt.setEnabled(False)

    def _on_log_line(self, key: str, text: str) -> None:
        if key == "psd":
            self.psd_opt.append_log(text)
        elif key == "fci":
            self.fci_opt.append_log(text)
        else:
            self.psd_opt.append_log(text)
            self.fci_opt.append_log(text)

    def _on_grid_result(self, key: str, result: FomResult) -> None:
        panel = self.psd_opt if key == "psd" else self.fci_opt
        panel.result.show_result(result)

    def _on_progress(self, key: str, completed: int, total: int) -> None:
        panel = self.psd_opt if key == "psd" else self.fci_opt
        panel.set_progress(completed, total)

    def _on_sweep_finished(self) -> None:
        self.btn_start_opt.setEnabled(self._client is not None)
        self.btn_stop_opt.setEnabled(False)
        self.psd_opt.set_running(False)
        self.fci_opt.set_running(False)
        self._sweep_worker = None

    def _on_sweep_error(self, message: str) -> None:
        QMessageBox.warning(self, "Optimization Error", message)
        self._on_sweep_finished()

    # --------------------------------------------------------------------------------- Compute tab

    def _build_compute_tab(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(QLabel("Fits the double-Gaussian to a fixed set of events and reports the FoM."))

        src_box = QGroupBox("Data source")
        src_layout = QHBoxLayout(src_box)
        self.radio_live = QRadioButton("Live session data")
        self.radio_live.setChecked(True)
        self.radio_file = QRadioButton("Load from CSV file...")
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._browse_file)
        self.btn_browse.setEnabled(False)
        self.lbl_source_status = QLabel("")
        src_layout.addWidget(self.radio_live)
        src_layout.addWidget(self.radio_file)
        src_layout.addWidget(self.btn_browse)
        src_layout.addWidget(self.lbl_source_status, stretch=1)
        layout.addWidget(src_box)
        # Enabling Browse belongs to radio_file's toggled signal, not radio_live's -- connecting
        # the wrong one left Browse disabled exactly when the user wanted to use it.
        self.radio_file.toggled.connect(self.btn_browse.setEnabled)

        panels_layout = QHBoxLayout()
        self.psd_panel = _ComputeDiscriminatorPanel("PSD")
        self.fci_panel = _ComputeDiscriminatorPanel("FCI")
        panels_layout.addWidget(self.psd_panel)
        panels_layout.addWidget(self.fci_panel)
        layout.addLayout(panels_layout)

        self.btn_run = QPushButton("Compute")
        self.btn_run.clicked.connect(self._run_compute)
        layout.addWidget(self.btn_run)

        results_layout = QHBoxLayout()
        self.psd_result = _ResultPanel("PSD", (255, 140, 0))
        self.fci_result = _ResultPanel("FCI", (0, 170, 255))
        # Capped so two side-by-side histograms plus everything above them fit inside the dialog's
        # fixed size without the tab's scroll area ever needing to activate.
        self.psd_result.plot.setMaximumHeight(220)
        self.fci_result.plot.setMaximumHeight(220)
        results_layout.addWidget(self.psd_result)
        results_layout.addWidget(self.fci_result)
        layout.addLayout(results_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, "Compute FoM (fixed dataset)")

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load Event CSV", "", "CSV files (*.csv)")
        if not path:
            return
        try:
            energy, fci, psd = _load_csv(Path(path))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Load Failed", str(e))
            return
        self.radio_file.setChecked(True)
        self._energy = np.asarray(energy, dtype=np.float64)
        self._fci = np.asarray(fci, dtype=np.float64)
        self._psd = np.asarray(psd, dtype=np.float64)
        self.lbl_source_status.setText(f"{Path(path).name}: {len(energy)} events loaded")

    def _load_active_source(self) -> bool:
        if self.radio_live.isChecked():
            energy, fci, psd = self._get_live_events()
            if len(energy) == 0:
                QMessageBox.warning(self, "No Data", "No live events accumulated yet in this "
                                                       "session -- start acquisition first, or "
                                                       "load a CSV file instead.")
                return False
            self._energy = np.asarray(energy, dtype=np.float64)
            self._fci = np.asarray(fci, dtype=np.float64)
            self._psd = np.asarray(psd, dtype=np.float64)
            self.lbl_source_status.setText(f"live session: {len(energy)} events")
            return True
        if self._energy is None:
            QMessageBox.warning(self, "No Data", "Browse to a CSV file first.")
            return False
        return True

    def _run_compute(self) -> None:
        if not self._load_active_source():
            return
        if not self.psd_panel.chk_enabled.isChecked() and not self.fci_panel.chk_enabled.isChecked():
            QMessageBox.information(self, "Nothing to Do", "Enable PSD, FCI, or both.")
            return

        self.psd_result.setVisible(self.psd_panel.chk_enabled.isChecked())
        self.fci_result.setVisible(self.fci_panel.chk_enabled.isChecked())

        if self.psd_panel.chk_enabled.isChecked():
            self._compute_one(self.psd_panel, self._psd, self.psd_result)
        if self.fci_panel.chk_enabled.isChecked():
            self._compute_one(self.fci_panel, self._fci, self.fci_result)

    def _compute_one(self, panel: _ComputeDiscriminatorPanel, values: np.ndarray,
                      result: _ResultPanel) -> None:
        lld = panel.energy_cut.spin_lld.value()
        uld = panel.energy_cut.uld()
        mask = self._energy >= lld
        if uld is not None:
            mask &= self._energy <= uld
        filtered = values[mask]
        try:
            r = compute_fom(filtered)
        except FomFitError as e:
            logger.warning(f"FoM fit failed: {e}")
            result.show_error(str(e))
            return
        result.show_result(r)

    # ------------------------------------------------------------------------------------- misc

    def closeEvent(self, event) -> None:
        if self._sweep_worker is not None and self._sweep_worker.isRunning():
            self._sweep_worker.request_stop()
            self._sweep_worker.wait(5000)
        super().closeEvent(event)
