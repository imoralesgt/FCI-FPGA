"""Offline FoM tuning of the FCI windows and PSD gates on recorded raw traces.

Scores with the GUI's own fom_core.compute_fom (double-Gaussian fit, FoM = S/(FWHM1+FWHM2)) so a
number found here means the same thing the instrument's Optimize tab would report.

Energy is the baseline-referenced PEAK AMPLITUDE, as in Morales et al. section 4.2.4, calibrated
by putting the 6Li(n,alpha)t capture peak at 3160 keVee -- the one feature in this spectrum whose
energy is known a priori. An LLD is then expressible in keVee rather than ADC counts.

The fit is GUARDED. An unconstrained grid search over a double-Gaussian FoM will happily find
degenerate optima: two nearly-coincident narrow Gaussians on one side of a single peak give a
large S/(FWHM1+FWHM2) while separating nothing. Every candidate must therefore put at least
MIN_FRACTION of events on each side of the midpoint between the fitted centroids.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from gui.fom_core import compute_fom, FomFitError
from analysis.fpga_model import fci_from_asdm, psd_from_traces

CAPTURE_KEVEE = 3160.0
MIN_FRACTION = 0.05


def energy_amplitude(traces: np.ndarray, pre: int = 90) -> np.ndarray:
    """Peak amplitude referenced to this trace's own pre-trigger baseline."""
    return traces.max(axis=1) - traces[:, :pre].mean(axis=1)


def calibrate(amp: np.ndarray) -> float:
    """keVee per ADC count, from the 6Li capture peak's centroid."""
    hi = amp[amp > 0.5 * np.percentile(amp, 99)]
    counts, edges = np.histogram(hi, bins=60)
    c = 0.5 * (edges[:-1] + edges[1:])
    return CAPTURE_KEVEE / c[counts.argmax()]


def guarded_fom(values: np.ndarray) -> float:
    """compute_fom, but returns -1 for degenerate fits (see module docstring)."""
    v = values[np.isfinite(values)]
    if len(v) < 60:
        return -1.0
    try:
        r = compute_fom(v)
    except (FomFitError, ValueError, RuntimeError):
        return -1.0
    if not np.isfinite(r.fom) or r.fom <= 0 or r.separation <= 0:
        return -1.0
    mid = 0.5 * (r.mu1 + r.mu2)
    frac = float((v < mid).mean())
    if frac < MIN_FRACTION or frac > 1 - MIN_FRACTION:
        return -1.0
    return float(r.fom)


def sweep_fci(mag, keep, grid_l, grid_w):
    best, rows = None, []
    for lhi in grid_l:
        for whi in grid_w:
            if whi <= lhi:
                continue
            f = guarded_fom(fci_from_asdm(mag, 1, lhi, 1, whi)[keep])
            if f > 0:
                rows.append((f, lhi, whi))
                if best is None or f > best[0]:
                    best = (f, lhi, whi)
    rows.sort(reverse=True)
    return best, rows


def sweep_psd(cum, keep, pre_trigger, grid_pg, grid_sg, grid_lg):
    best, rows = None, []
    for pg in grid_pg:
        for sg in grid_sg:
            for lg in grid_lg:
                if lg <= sg:
                    continue
                f = guarded_fom(psd_from_traces(cum, pre_trigger, pg, sg, lg)[keep])
                if f > 0:
                    rows.append((f, pg, sg, lg))
                    if best is None or f > best[0]:
                        best = (f, pg, sg, lg)
    rows.sort(reverse=True)
    return best, rows


# --------------------------------------------------------------- supervised scoring
#
# The unsupervised double-Gaussian fit is what the instrument computes, but on this dataset the
# gamma population is only ~200 events against ~900 capture events, and the fit is correspondingly
# unstable (its FoM jumped between 0.03 and 0.82 over neighbouring LLD values). So the SEARCH is
# driven by a supervised score using an energy-based labelling that rests on known physics:
#
#   neutron-like : the 6Li(n,alpha)t capture peak, 2800-3500 keVee
#   gamma-like   : continuum well below it, 500-2000 keVee
#
# This is not circular -- energy and pulse shape are independent observables, and it is the same
# approach the paper takes with its Cs-137-only and mixed datasets. It is APPROXIMATE: the Compton
# continuum of high-energy gammas reaches into the capture window, so the neutron group carries
# some gamma contamination, which makes the resulting FoM a lower bound rather than an ideal.
#
# Widths come from the IQR (sigma = IQR/1.349) rather than a fit: robust to the tails that a
# ratio-of-integrals discriminator always has, and fast enough to scan a six-figure grid.

N_BAND_KEVEE = (2800.0, 3500.0)
G_BAND_KEVEE = (500.0, 2000.0)
IQR_TO_SIGMA = 1.349
FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))


def energy_labels(E: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = (E >= N_BAND_KEVEE[0]) & (E <= N_BAND_KEVEE[1])
    g = (E >= G_BAND_KEVEE[0]) & (E <= G_BAND_KEVEE[1])
    return n, g


def fom_supervised(values: np.ndarray, n_mask: np.ndarray, g_mask: np.ndarray) -> float:
    """|median_n - median_g| / (FWHM_n + FWHM_g), widths from the IQR."""
    a, b = values[n_mask], values[g_mask]
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 30 or len(b) < 30:
        return -1.0
    qa, qb = np.percentile(a, [25, 75]), np.percentile(b, [25, 75])
    sa, sb = (qa[1] - qa[0]) / IQR_TO_SIGMA, (qb[1] - qb[0]) / IQR_TO_SIGMA
    denom = FWHM_PER_SIGMA * (sa + sb)
    if denom <= 0:
        return -1.0
    return float(abs(np.median(a) - np.median(b)) / denom)
