"""Background QThread running the FoM "Optimize" grid search: for each selected discriminator
(PSD and/or FCI), for each of its selected sweep parameters -- independently, one at a time, in a
coordinate-wise scan rather than a combinatorial joint grid -- writes a range of values to the
device, collects fresh live events at each one, scores the resulting FoM, and writes back whichever
value scored best before moving on to the next parameter.

This can only run against live hardware, not previously recorded or already-accumulated data: the
whole point is to measure how a REAL parameter change affects REAL separation, which a static
dataset cannot answer -- you cannot ask "what would the FoM have been with short_gate=64" from
data that was captured with short_gate=80.

Runs on its own thread because each grid point involves real, seconds-scale waiting for live
events -- this must never block the GUI event loop. Needs the normal AcquisitionWorker's batch
polling suspended for the duration (see AcquisitionWorker.suspend_batch_polling()'s docstring):
otherwise both would race $RB for the same 32-deep FIFO, and "collect N fresh events for this grid
point" would be unreliable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QThread, Signal

from fci_api import AcqEvent, FciClient, FciError
from fom_core import FomFitError, FomResult, SweepParam, compute_fom

logger = logging.getLogger(__name__)

EVENT_COLLECT_TIMEOUT_S = 30.0
"""Per grid point: collect up to `events_per_point` events, but never wait longer than this for a
single point -- a misconfigured trigger or a parameter value that happens to silence the detector
must not be able to hang the whole sweep indefinitely."""


@dataclass
class DiscriminatorSweepPlan:
    enabled: bool
    lld: float
    uld: float | None
    params: list[SweepParam]
    set_fn_name: str  # "set_psd" or "set_fci"
    value_field: str  # "psd" or "fci" -- which AcqEvent attribute this discriminator reads


@dataclass
class SweepPlan:
    steps: int
    events_per_point: int
    discriminators: dict[str, DiscriminatorSweepPlan] = field(default_factory=dict)
    calibration: tuple[float, float, float] = (0.0, 1.0, 0.0)
    """(c0, c1, c2) from HistogramView -- the same session-wide coefficients live_view's
    FCI/PSD-vs-Energy plots and the Spectrum tab apply to `peak`. One set for the whole plan, not
    per-discriminator, because it is one calibration for one session. Defaults to the identity map
    so a caller that never wired it through still filters on raw peak, same as before this field
    existed."""


class FomSweepWorker(QThread):
    log_line = Signal(str, str)  # (discriminator_key, text)
    grid_result = Signal(str, object)  # (discriminator_key, FomResult) -- live plot update
    progress = Signal(str, int, int)  # (discriminator_key, completed, total) -- for a progress bar
    finished_all = Signal()
    error = Signal(str)

    def __init__(self, client: FciClient, acquisition_worker, plan: SweepPlan):
        super().__init__()
        self._client = client
        self._acq_worker = acquisition_worker
        self._plan = plan
        self._stop_requested = False

    def request_stop(self) -> None:
        """Checked between grid points and between parameters -- safe to call from any thread.
        A sweep in progress finishes its CURRENT grid point (a partial write left on the device
        would be worse than a slightly late stop) rather than aborting mid-measurement."""
        self._stop_requested = True

    def run(self) -> None:
        self._acq_worker.suspend_batch_polling()
        try:
            self._client.enable_acquisition()
            for key, disc in self._plan.discriminators.items():
                if not disc.enabled or self._stop_requested:
                    continue
                self._sweep_discriminator(key, disc)
            if self._stop_requested:
                self.log_line.emit("", "Optimization stopped by user.")
            else:
                self.log_line.emit("", "Optimization complete.")
            self.finished_all.emit()
        except FciError as e:
            logger.error(f"FoM sweep failed: {e}")
            self.error.emit(str(e))
        finally:
            self._acq_worker.resume_batch_polling()

    def _sweep_discriminator(self, key: str, disc: DiscriminatorSweepPlan) -> None:
        set_fn = getattr(self._client, disc.set_fn_name)

        # Grids are precomputed for every selected parameter up front, purely so the total point
        # count -- and with it, a progress bar -- is known before the sweep starts. linspace can
        # collapse to fewer than `steps` distinct integers on a narrow range, so this has to be the
        # actual grid length, not just len(params) * steps.
        grids = {
            param.name: np.unique(np.round(
                np.linspace(param.minimum, param.maximum, self._plan.steps)
            ).astype(int))
            for param in disc.params
        }
        total = sum(len(g) for g in grids.values())
        completed = 0
        self.progress.emit(key, completed, total)

        for param in disc.params:
            if self._stop_requested:
                return
            self.log_line.emit(key, f"--- Sweeping {param.label} "
                                     f"[{param.minimum}, {param.maximum}] ---")
            grid = grids[param.name]

            best_value: int | None = None
            best_fom = -np.inf
            for v in grid:
                if self._stop_requested:
                    return
                try:
                    set_fn(**{param.name: int(v)})
                    self._client.reset()  # drains stale FIFO/stats from before this write
                except FciError as e:
                    self.log_line.emit(key, f"{param.label}={v}: write failed ({e}), skipping")
                    completed += 1
                    self.progress.emit(key, completed, total)
                    continue

                events = self._collect_events(self._plan.events_per_point)
                values = self._filter(events, disc, self._plan.calibration)
                if len(values) == 0:
                    self.log_line.emit(key, f"{param.label}={v}: 0 usable events, skipping")
                    completed += 1
                    self.progress.emit(key, completed, total)
                    continue
                try:
                    r = compute_fom(values)
                except FomFitError as e:
                    self.log_line.emit(key, f"{param.label}={v}: n={len(values)}, fit failed ({e})")
                    completed += 1
                    self.progress.emit(key, completed, total)
                    continue

                self.grid_result.emit(key, r)
                marker = ""
                if r.fom > best_fom:
                    best_fom, best_value = r.fom, int(v)
                    marker = "  <- best so far"
                self.log_line.emit(
                    key, f"{param.label}={v}: n={len(values)}  FoM={r.fom:.4f}{marker}"
                )
                completed += 1
                self.progress.emit(key, completed, total)

            if best_value is not None:
                try:
                    set_fn(**{param.name: best_value})
                    self._client.reset()
                except FciError as e:
                    self.log_line.emit(key, f"Could not apply best {param.label}={best_value}: {e}")
                    continue
                self.log_line.emit(
                    key, f"Best {param.label} = {best_value} (FoM={best_fom:.4f}) -- applied.\n"
                )
            else:
                self.log_line.emit(key, f"No usable FoM found while sweeping {param.label} -- "
                                         f"left unchanged.\n")

    def _collect_events(self, n_target: int) -> list[AcqEvent]:
        collected: list[AcqEvent] = []
        deadline = time.monotonic() + EVENT_COLLECT_TIMEOUT_S
        while len(collected) < n_target and time.monotonic() < deadline:
            if self._stop_requested:
                break
            batch = self._client.read_batch(32)
            collected.extend(batch)
            if not batch:
                time.sleep(0.05)
        return collected

    @staticmethod
    def _filter(events: list[AcqEvent], disc: DiscriminatorSweepPlan,
                calibration: tuple[float, float, float]) -> np.ndarray:
        """LLD/ULD select on CALIBRATED energy, `c0 + c1*peak + c2*peak^2` (see AcqEvent.peak),
        rather than `energy_long` -- the same energy channel the Spectrum tab histograms and
        live_view's FCI/PSD-vs-Energy plots use, through the SAME coefficients (SweepPlan.calibration,
        sourced from HistogramView), so a cut set here means the same energy region there.
        `energy_long <= 0` remains a SEPARATE validity guard, not an energy-region cut: it excludes
        the low-energy BLR pathology (project log section 8d) where the long-gate integral goes
        non-positive and firmware's PSD ratio is a 0.0 "undefined" sentinel, not a real
        measurement -- unrelated to which amplitude range LLD/ULD ask for."""
        c0, c1, c2 = calibration
        out = []
        for e in events:
            if e.energy_long <= 0:
                continue
            energy = c0 + c1 * e.peak + c2 * e.peak * e.peak
            if energy < disc.lld:
                continue
            if disc.uld is not None and energy > disc.uld:
                continue
            out.append(getattr(e, disc.value_field))
        return np.asarray(out, dtype=np.float64)
