"""Pure figure-of-merit computation: no Qt, no device I/O. Shared by fom_wizard.py (the
single-shot "Compute FoM" tab) and fom_sweep_worker.py (the "Optimize" grid search), so both score
a population of events with exactly the same fitting logic.

Methodology: collapse a discriminator's (PSD or FCI) values into a 1D histogram and fit it as the
sum of two Gaussians; FoM = S / (FWHM_1 + FWHM_2), where S is the distance between the two fitted
centroids. Which events go in is the caller's job (an LLD/ULD energy cut, typically) -- this module
only fits whatever array it's handed.

Peak seeding is fully automatic (scipy.signal.find_peaks on a lightly smoothed histogram, falling
back to a median split if it can't find two distinct peaks) rather than needing a user-supplied
division value: the "Optimize" grid search evaluates this at every point of a sweep, so it cannot
depend on the user manually re-seeding it each time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import curve_fit
from scipy.signal import find_peaks

HIST_BINS = 100


@dataclass(frozen=True)
class SweepParam:
    name: str
    """set_psd()/set_fci() keyword argument name."""
    label: str
    minimum: int
    maximum: int


PSD_SWEEP_PARAMS = [
    SweepParam("pre_gate", "Pre-gate", 0, 2048),
    SweepParam("short_gate", "Short gate", 0, 2048),
    SweepParam("long_gate", "Long gate", 0, 2048),
]
"""pre_trigger is excluded: it is locked to the Trigger tab's Delay, not a discrimination
knob. baseline_ref is excluded too: it is a pedestal trim, not a pulse-shape parameter -- see its
own docstring in fci_api/types.py. These three are explicitly what acquisition.c itself calls "the
discrimination knobs ... meant to be swept" (PSD_PRE_GATE/SHORT_GATE/LONG_GATE's own comment)."""

FCI_SWEEP_PARAMS = [
    SweepParam("psa_l_lo", "PSA_l low", 0, 1024),
    SweepParam("psa_l_hi", "PSA_l high", 0, 1024),
    SweepParam("psa_w_lo", "PSA_w low", 0, 1024),
    SweepParam("psa_w_hi", "PSA_w high", 0, 1024),
]


def _double_gaussian(x, a1, mu1, sigma1, a2, mu2, sigma2):
    return (a1 * np.exp(-((x - mu1) ** 2) / (2 * sigma1**2))
            + a2 * np.exp(-((x - mu2) ** 2) / (2 * sigma2**2)))


@dataclass
class FomResult:
    n_events: int
    bin_centers: np.ndarray
    counts: np.ndarray
    fit_curve: np.ndarray
    mu1: float
    fwhm1: float
    mu2: float
    fwhm2: float
    separation: float
    fom: float


class FomFitError(Exception):
    pass


def _auto_seed(values: np.ndarray, counts: np.ndarray,
                centers: np.ndarray) -> tuple[float, float, float, float]:
    """Returns (mu1, sigma1, mu2, sigma2) initial guesses for the two-Gaussian fit, found from the
    histogram itself -- see module docstring."""
    kernel = np.ones(5) / 5.0
    smoothed = np.convolve(counts, kernel, mode="same")
    min_distance = max(1, len(counts) // 20)
    peak_idx, _ = find_peaks(smoothed, distance=min_distance,
                              prominence=max(float(smoothed.max()) * 0.03, 1.0))

    if len(peak_idx) >= 2:
        order = np.argsort(smoothed[peak_idx])[::-1]
        i1, i2 = sorted(peak_idx[order[:2]])
        mu1, mu2 = float(centers[i1]), float(centers[i2])
        gap = abs(mu2 - mu1)
        span = float(centers[-1] - centers[0]) or 1.0
        sigma_guess = max(gap / 6.0, span / 40.0)
        return mu1, sigma_guess, mu2, sigma_guess

    # Fewer than two distinguishable peaks in the histogram -- fall back to splitting the raw
    # data at its median and seeding from each half's own mean/std.
    median = float(np.median(values))
    low, high = values[values < median], values[values >= median]
    if len(low) < 5 or len(high) < 5:
        raise FomFitError(
            "could not find two separable populations in this data (no two histogram peaks, and "
            "a median split doesn't separate it either)"
        )
    return (float(np.mean(low)), max(float(np.std(low)), 1e-6),
            float(np.mean(high)), max(float(np.std(high)), 1e-6))


def compute_fom(values: np.ndarray) -> FomResult:
    """Fits a sum of two Gaussians to `values`' histogram and returns the FoM. Raises FomFitError
    if there isn't enough data, no two-peak structure can be found, or the fit doesn't converge."""
    if len(values) < 20:
        raise FomFitError(f"only {len(values)} events -- too few to fit")

    counts, edges = np.histogram(values, bins=HIST_BINS)
    centers = 0.5 * (edges[:-1] + edges[1:])

    mu1_g, sigma1_g, mu2_g, sigma2_g = _auto_seed(values, counts, centers)
    p0 = [counts.max(), mu1_g, sigma1_g, counts.max(), mu2_g, sigma2_g]
    span = float(values.max() - values.min()) or 1.0
    bounds_lo = [0, values.min(), 1e-9, 0, values.min(), 1e-9]
    bounds_hi = [np.inf, values.max(), span, np.inf, values.max(), span]

    try:
        popt, _ = curve_fit(_double_gaussian, centers, counts, p0=p0,
                             bounds=(bounds_lo, bounds_hi), maxfev=20000)
    except RuntimeError as e:
        raise FomFitError(f"double-Gaussian fit did not converge: {e}") from e

    a1, mu1, sigma1, a2, mu2, sigma2 = popt
    if mu2 < mu1:
        # Report the lower-centroid peak first, purely for consistent, readable output -- the fit
        # itself doesn't care about ordering.
        mu1, sigma1, mu2, sigma2 = mu2, sigma2, mu1, sigma1

    k = 2.0 * np.sqrt(2.0 * np.log(2.0))
    fwhm1, fwhm2 = k * abs(sigma1), k * abs(sigma2)
    separation = abs(mu2 - mu1)
    denom = fwhm1 + fwhm2
    if denom <= 0:
        raise FomFitError("fitted peaks have zero width -- cannot compute a FoM")

    return FomResult(
        n_events=len(values), bin_centers=centers, counts=counts,
        fit_curve=_double_gaussian(centers, *popt),
        mu1=mu1, fwhm1=fwhm1, mu2=mu2, fwhm2=fwhm2,
        separation=separation, fom=separation / denom,
    )
