"""Trace preprocessing: reject saturated, malformed and piled-up captures, and (for the charge-
comparison path) align each pulse on its own rising-edge CFD point.

Why this module exists: docs/log/README.md 8r. Computing PSD straight off the raw captured traces --
no quality cut, no pile-up rejection, no alignment -- gives a gamma/neutron separation of ~0.6 sigma,
which reads as "PSD does not work on this detector" and is wrong. The paper does the preprocessing
this project had skipped: section 4.2.3 removes pile-up and saturated traces, and section 4.3 removes
the baseline and aligns pulses by CFD "to ensure precise charge estimation for the CCM".

FCI needs the event-quality cut but NOT the alignment -- an FFT magnitude is shift-invariant, so
trigger walk and jitter cannot move it. That asymmetry is exactly the "no preprocessing" property the
paper claims for the method, and it is why PSD was the only one of the two that looked broken here.

Two calibration notes, both learned the hard way (8r):
  * The pre-pulse search window must END BEFORE the rising edge. The hardware CFD fires ~32 samples
    into the rise, so a window anchored near the trigger point contains most of the rise itself and
    flags every trace as piled-up.
  * The tail-rebound threshold must scale with the BASELINE NOISE, not only with pulse height. At
    30 counts RMS, a fixed 8%-of-peak threshold is meaningless for a 216-count pulse and rejects
    ordinary noise wiggle as a second pulse.
"""
from __future__ import annotations

import numpy as np

CFD_FRACTION = 0.5
REF_INDEX = 50
"""Aligned traces put the rising-edge CFD_FRACTION crossing here. 50, not the hardware's own
pre_trigger=100, because the crossing sits near sample 82 in the raw captures -- a smaller reference
keeps every shift positive so no trace needs padding."""
WINDOW = 900
SEARCH_LIMIT = 400
"""argmax is taken over the first SEARCH_LIMIT samples only: a piled-up pulse later in the
2048-sample capture would otherwise win argmax and put the "peak" hundreds of samples away."""
CROSSING_RANGE = (60.0, 140.0)
SATURATION_COUNTS = 12000.0
"""Below the measured 12602-count ceiling, with margin -- a clipped pulse has no usable shape."""

PRE_PULSE_END = 55          # strictly before the rising edge starts (~sample 60-65)
PRE_PULSE_FRAC = 0.10
TAIL_SPAN = 20              # samples over which a second pulse's rise is measured
TAIL_END = 400
TAIL_FRAC = 0.08
NOISE_SIGMA_MULT = 5.0
SMOOTH_K = 9


def _smoothed(traces: np.ndarray) -> np.ndarray:
    ker = np.ones(SMOOTH_K) / SMOOTH_K
    return np.apply_along_axis(lambda r: np.convolve(r, ker, mode="same"), 1, traces)


def quality_mask(traces: np.ndarray) -> np.ndarray:
    """True for events to KEEP: not saturated, not malformed, no detectable pile-up.

    Pile-up shows up two ways, and both are checked: a previous pulse's tail still present in the
    pre-trigger region, and a second pulse rising out of this one's decay. Thresholds are the larger
    of a fraction of pulse height and a multiple of that trace's own baseline noise.
    """
    smooth = _smoothed(traces)
    sigma = traces[:, 5:50].std(axis=1)
    peak = smooth[:, :SEARCH_LIMIT].max(axis=1)
    peak_idx = smooth[:, :SEARCH_LIMIT].argmax(axis=1)
    keep = np.ones(len(traces), dtype=bool)
    for i in range(len(traces)):
        p = peak[i]
        if p <= 0 or traces[i].max() >= SATURATION_COUNTS or peak_idx[i] < 20:
            keep[i] = False
            continue
        if smooth[i, :PRE_PULSE_END].max() > max(PRE_PULSE_FRAC * p, NOISE_SIGMA_MULT * sigma[i]):
            keep[i] = False
            continue
        a, b = peak_idx[i] + 20, min(traces.shape[1], peak_idx[i] + TAIL_END)
        if b - a > TAIL_SPAN:
            seg = smooth[i, a:b]
            rise = seg[TAIL_SPAN:] - seg[:-TAIL_SPAN]
            if rise.max() > max(TAIL_FRAC * p, NOISE_SIGMA_MULT * sigma[i]):
                keep[i] = False
    return keep


def align(traces: np.ndarray, shift: bool = True):
    """Returns (aligned[n, WINDOW], mask) -- each kept trace re-indexed so its rising-edge CFD
    crossing sits at REF_INDEX. shift=False applies the same acceptance but keeps the hardware
    CFD's own indexing, which is the control that isolates how much the alignment itself is worth.
    """
    out, keep = [], np.zeros(len(traces), dtype=bool)
    for i, t in enumerate(traces):
        peak_idx = int(np.argmax(t[:SEARCH_LIMIT]))
        peak = t[peak_idx]
        if peak <= 0 or peak_idx < 20:
            continue
        thr = CFD_FRACTION * peak
        rise = t[:peak_idx + 1]
        idx = int(np.argmax(rise >= thr))
        if idx <= 0:
            continue
        y0, y1 = rise[idx - 1], rise[idx]
        crossing = (idx - 1) + (thr - y0) / (y1 - y0) if y1 > y0 else float(idx)
        if not (CROSSING_RANGE[0] < crossing < CROSSING_RANGE[1]):
            continue
        start = (int(round(crossing)) - REF_INDEX) if shift else (100 - REF_INDEX)
        if start < 0 or start + WINDOW > len(t):
            continue
        out.append(t[start:start + WINDOW])
        keep[i] = True
    return (np.array(out) if out else np.empty((0, WINDOW))), keep


def psd(prepared: np.ndarray, pre_gate: int, short_gate: int, long_gate: int) -> np.ndarray:
    """(long - short) / long on aligned traces, gates opening at REF_INDEX - pre_gate."""
    gs = REF_INDEX - pre_gate
    if gs < 0:
        return np.full(len(prepared), np.nan)
    short = prepared[:, gs:gs + short_gate].sum(axis=1)
    long_ = prepared[:, gs:gs + long_gate].sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(long_ > 0, (long_ - short) / long_, np.nan)
