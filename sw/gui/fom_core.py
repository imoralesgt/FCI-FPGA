"""Pure figure-of-merit computation: no Qt, no device I/O. Shared by fom_wizard.py (the
single-shot "Compute FoM" tab) and fom_sweep_worker.py (the "Optimize" grid search), so both score
a population of events with exactly the same fitting logic.

Methodology: collapse a discriminator's (PSD or FCI) values into a 1D histogram and fit it as the
sum of two Gaussians; FoM = S / (FWHM_1 + FWHM_2), where S is the distance between the two fitted
centroids. Which events go in is the caller's job (an LLD/ULD energy cut, typically) -- this module
only fits whatever array it's handed.

Peak seeding is automatic by default (scipy.signal.find_peaks on a lightly smoothed histogram,
falling back to a median split if it can't find two distinct peaks): the "Optimize" grid search
evaluates this at every point of a sweep, so it cannot depend on the user re-seeding it each time.
The single-shot "Compute FoM" tab passes the operator's g/n divider instead (compute_fom's
`divider`): each population is seeded from its own side of the line, and the result also reports
how the line classifies the data (class counts) and how much of each fitted Gaussian lies on the
wrong side of it -- the fitted counterpart of the leakage the hardware's straight line produces.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import curve_fit
from scipy.special import erfc
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
    divider: float | None = None
    """The g/n divider the fit was seeded from, if one was given."""
    n_below: int = 0
    """Events at or below the divider (class 0) -- 0 when no divider was given."""
    n_above: int = 0
    """Events above the divider (class 1)."""
    lower_above_frac: float = 0.0
    """Fraction of the lower fitted Gaussian (peak 1) lying above the divider: the fit's estimate
    of the class-0 population a line at the divider would put in class 1."""
    upper_below_frac: float = 0.0
    """Fraction of the upper fitted Gaussian (peak 2) lying at or below the divider."""


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


def _divider_seed(values: np.ndarray, divider: float) -> tuple[float, float, float, float]:
    """(mu1, sigma1, mu2, sigma2) seeds from each side of the divider: class 0 (<= divider) and
    class 1 (> divider), each from its own median and IQR-based width -- robust to the long tails
    either side of a real g/n line."""
    low, high = values[values <= divider], values[values > divider]
    if len(low) < 5 or len(high) < 5:
        raise FomFitError(
            f"the divider at {divider:.3f} leaves {len(low)} event(s) below and {len(high)} above "
            "-- move it between the two populations"
        )

    def med_sigma(v: np.ndarray) -> tuple[float, float]:
        q1, q3 = np.percentile(v, [25, 75])
        return float(np.median(v)), max(float(q3 - q1) / 1.349, 1e-6)

    m1, s1 = med_sigma(low)
    m2, s2 = med_sigma(high)
    return m1, s1, m2, s2


def compute_fom(values: np.ndarray, divider: float | None = None) -> FomResult:
    """Fits a sum of two Gaussians to `values`' histogram and returns the FoM. With `divider`, the
    two Gaussians are seeded from the events either side of it (class 0 = at or below, class 1 =
    above) and the result carries the class counts and each fitted Gaussian's fraction on the wrong
    side of the line; without it, seeding is automatic (see module docstring). Raises FomFitError
    if there isn't enough data, no two-peak structure can be found, or the fit doesn't converge."""
    if len(values) < 20:
        raise FomFitError(f"only {len(values)} events -- too few to fit")

    counts, edges = np.histogram(values, bins=HIST_BINS)
    centers = 0.5 * (edges[:-1] + edges[1:])

    if divider is not None:
        mu1_g, sigma1_g, mu2_g, sigma2_g = _divider_seed(values, divider)
    else:
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

    extra = {}
    if divider is not None:
        s1, s2 = abs(sigma1), abs(sigma2)
        extra = dict(
            divider=float(divider),
            n_below=int(np.count_nonzero(values <= divider)),
            n_above=int(np.count_nonzero(values > divider)),
            lower_above_frac=float(0.5 * erfc((divider - mu1) / (np.sqrt(2.0) * s1))),
            upper_below_frac=float(0.5 * erfc((mu2 - divider) / (np.sqrt(2.0) * s2))),
        )
    return FomResult(
        n_events=len(values), bin_centers=centers, counts=counts,
        fit_curve=_double_gaussian(centers, *popt),
        mu1=mu1, fwhm1=fwhm1, mu2=mu2, fwhm2=fwhm2,
        separation=separation, fom=separation / denom, **extra,
    )
