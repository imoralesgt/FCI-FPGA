"""Python model of the FCI/PSD discrimination the FPGA performs, for offline tuning on
recorded raw traces.

Emulates the two RTL cores exactly as they are written, so a window/gate found here can be
written straight to the device:

  fci_core_rtl/bin_accumulator.vhd
      2048-point FFT, approximate spectral density magnitude ASDM[k] = |Re| + |Im| (the
      city-block approximation -- no square root, no squaring), summed over two INCLUSIVE bin
      ranges. The RTL bit-reverses the FFT's output index to recover k; numpy already returns
      natural order, so that step has no analogue here.
      Block floating point in the IP scales every bin of a frame by one shared exponent, and FCI
      is a ratio of two sums over the same frame, so the scale cancels and float is exact for
      this purpose (project log section 8g measured the built core against float: IQR ratio 0.988).

  psd_core/dual_gate_integrator.vhd
      gate_start = max(0, pre_trigger - pre_gate); both gates open there.
      in_short: gate_start <= i < gate_start + short_gate   (half-open, per the RTL)
      in_long:  gate_start <= i < gate_start + long_gate

Index conventions follow the host API, not the papers: fci = PSA_l / PSA_w and
psd = (long - short) / long, matching AcqEvent in fci_api/types.py. Both are monotonic
transforms of the published definitions, so FoM is unchanged.
"""

from __future__ import annotations

import numpy as np

FFT_LENGTH = 2048


def load_traces(paths) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Reads scope-trace CSVs. Returns (traces[n, 2048], captured_length[n], source_index[n]).

    A capture SHORTER than FFT_LENGTH is zero-padded up to it, because that is exactly what
    sample_framer.vhd does on the way into the FFT (its header note 3): the frame boundary belongs
    to the framer, not to the producer, and a short capture is padded rather than dropped. So a
    depth-1024 recording emulated this way is what the hardware actually transformed, not an
    approximation of it. The padding is exact zeros and therefore contributes no noise, which is
    why a shorter capture is quieter per bin rather than merely shorter.
    """
    rows, lens, src = [], [], []
    for si, p in enumerate(paths):
        with open(p) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split(",")
                if len(parts) < 3:
                    continue
                n = int(parts[1])
                if n > FFT_LENGTH or len(parts) < 2 + n:
                    continue
                v = np.asarray(parts[2:2 + n], dtype=np.float64)
                if n < FFT_LENGTH:
                    v = np.concatenate([v, np.zeros(FFT_LENGTH - n)])
                rows.append(v); lens.append(n); src.append(si)
    if not rows:
        raise SystemExit("no traces found")
    return np.vstack(rows), np.asarray(lens), np.asarray(src)


def asdm(traces: np.ndarray) -> np.ndarray:
    """Per-trace approximate spectral density magnitude, bins 0..1024 (DC..Nyquist).

    Computed once; every candidate FCI window is then just a pair of cumulative-sum lookups.
    """
    spec = np.fft.rfft(traces, n=FFT_LENGTH, axis=1)      # 1025 bins, 0..Nyquist
    return np.abs(spec.real) + np.abs(spec.imag)


def fci_from_asdm(mag: np.ndarray, l_lo: int, l_hi: int, w_lo: int, w_hi: int) -> np.ndarray:
    """PSA_l / PSA_w over INCLUSIVE bin ranges, matching bin_accumulator's comparisons."""
    c = np.cumsum(mag, axis=1)
    def band(lo, hi):
        hi = min(hi, mag.shape[1] - 1)
        return c[:, hi] - (c[:, lo - 1] if lo > 0 else 0.0)
    psa_l, psa_w = band(l_lo, l_hi), band(w_lo, w_hi)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(psa_w > 0, psa_l / psa_w, np.nan)


def psd_from_traces(cum: np.ndarray, pre_trigger: int, pre_gate: int,
                    short_gate: int, long_gate: int, baseline_ref: int = 0) -> np.ndarray:
    """(long - short) / long, with the RTL's gate placement and half-open ends.

    `cum` is a prefix-sum of the traces with a leading zero column, so any gate is one
    subtraction -- what makes an exhaustive gate sweep affordable.
    """
    n = cum.shape[1] - 1
    gs = max(0, pre_trigger - pre_gate)
    se, le = min(n, gs + short_gate), min(n, gs + long_gate)
    short = cum[:, se] - cum[:, gs]
    long_ = cum[:, le] - cum[:, gs]
    if baseline_ref:
        short = short - baseline_ref * (se - gs)
        long_ = long_ - baseline_ref * (le - gs)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(long_ > 0, (long_ - short) / long_, np.nan)


def prefix_sums(traces: np.ndarray) -> np.ndarray:
    z = np.zeros((traces.shape[0], 1))
    return np.concatenate([z, np.cumsum(traces, axis=1)], axis=1)
