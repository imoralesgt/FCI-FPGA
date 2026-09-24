"""Compares this project's own low-light-output PSD/FCI behavior against Nakhostin's central
finding (M. Nakhostin, *Nucl. Instrum. Methods A* 916 (2019) 66-70, docs/log/README.md References
and 0): that a charge-comparison PSD parameter collapses under down-sampling (his Fig. 5 -- "failed
events" dislocated to the top of the plot below ~200 keVee at 32 MHz, discrimination *completely*
lost there), while a frequency-domain index degrades only gracefully over the same range (his Fig. 7
-- clean, unbroken neutron/gamma bands from 4 GHz all the way down to 32 MHz, FoM 0.75->0.62).

This project's own two metrics are the direct analogues: PSD is a charge-comparison ratio
((long-short)/long, dual_gate_integrator.vhd) and FCI is a frequency-domain ratio (a low-band-over-
total-band ASDM ratio, bin_accumulator.vhd -- docs/log/README.md 0's own point that this is "the
same idea" as Nakhostin's index, differing in normalization). At 50 Msps this instrument sits above
Nakhostin's 32 MHz "collapse" floor (0's own "why this instrument sits above both floors"), so
naively neither method should show his Fig. 5 failure mode at all -- but the grid-search-optimal
PSD configuration's very narrow short_gate=5 (100 ns) turns out to reproduce it anyway (quantified
below), which is itself informative: it is the GATE WIDTH relative to the pulse, not the raw ADC
sampling rate alone, that determines whether charge comparison survives.

Uses the same two datasets docs/log/README.md 8v's live-hardware confirmation already established:
    offline: sw/analysis/plot_cosmics_optimal_psd_fci.py's raw-trace recomputation
    live:    sw/analysis/plot_cosmics_live_optimal.py's real on-hardware run

Run: sw/.venv/bin/python -m analysis.plot_nakhostin_comparison   (from sw/)
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors
import matplotlib.pyplot as plt
import numpy as np

from analysis.plot_cosmics_live_optimal import load_dataset as load_live
from analysis.plot_cosmics_optimal_psd_fci import load_dataset as load_offline

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

# Nakhostin's own Fig. 5/7 x-axis range and dashed threshold marker, reused here so the panels are
# visually comparable at a glance, not just numerically.
LIGHT_OUTPUT_RANGE_KEVEE = (0.0, 1400.0)
NAKHOSTIN_THRESHOLD_KEVEE = 200.0

REFERENCE_BAND_KEVEE = (2000.0, 3000.0)  # clean, high-statistics band used to define "normal" PSD
DISLOCATION_SIGMA = 5.0


def quantify_dislocation(psd: np.ndarray, keVee: np.ndarray, bins_kevee):
    ref = (keVee >= REFERENCE_BAND_KEVEE[0]) & (keVee <= REFERENCE_BAND_KEVEE[1])
    med_ref = np.median(psd[ref])
    q = np.percentile(psd[ref], [25, 75])
    sig_ref = (q[1] - q[0]) / 1.349
    rows = []
    for lo, hi in zip(bins_kevee[:-1], bins_kevee[1:]):
        m = (keVee >= lo) & (keVee < hi)
        if m.sum() == 0:
            continue
        p = psd[m]
        dislocated = np.abs(p - med_ref) > DISLOCATION_SIGMA * sig_ref
        rows.append((lo, hi, int(m.sum()), float(dislocated.mean() * 100)))
    return rows, med_ref, sig_ref


def plot_comparison(fci_o, psd_o, keVee_o, fci_l, psd_l, keVee_l):
    fig, axes = plt.subplots(2, 2, figsize=(15, 10), dpi=140, sharex=True)
    lo, hi = LIGHT_OUTPUT_RANGE_KEVEE
    panels = [
        (axes[0, 0], psd_o, keVee_o, "PSD (offline-optimal)", (0.6, 1.1)),
        (axes[0, 1], psd_l, keVee_l, "PSD (live hardware)", (0.6, 1.1)),
        (axes[1, 0], fci_o, keVee_o, "FCI (offline-optimal)", (0.4, 1.0)),
        (axes[1, 1], fci_l, keVee_l, "FCI (live hardware)", (0.4, 1.0)),
    ]
    for ax, Y, keVee, title, ylim in panels:
        m = (keVee >= lo) & (keVee <= hi)
        h = ax.hist2d(keVee[m], Y[m], bins=[200, 200], range=[[lo, hi], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm())
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvline(NAKHOSTIN_THRESHOLD_KEVEE, color="black", linestyle="--", linewidth=1.2)
        ax.set_xlabel("Light output / Energy (keVee)")
        ax.set_ylabel(title)
        ax.set_title(f"{title} vs Energy, 0-1,400 keVee (Nakhostin Fig. 5/7's own x-range)")
    plt.tight_layout()
    out = OUT_DIR / "nakhostin_comparison_psd_fci_low_energy.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=== offline-optimal ===")
    fci_o, psd_o, keVee_o = load_offline()
    print("=== live hardware ===")
    fci_l, psd_l, keVee_l = load_live()

    bins_kevee = [97, 150, 200, 300, 400, 600, 900, 1400]
    print(f"\nPSD dislocation (fraction >|{DISLOCATION_SIGMA}sigma| from the "
          f"{REFERENCE_BAND_KEVEE} keVee reference band's own median/IQR):")
    for name, psd, keVee in [("offline", psd_o, keVee_o), ("live", psd_l, keVee_l)]:
        rows, med_ref, sig_ref = quantify_dislocation(psd, keVee, bins_kevee)
        print(f"  {name}: reference median={med_ref:.4f} sigma={sig_ref:.4f}")
        for lo, hi, n, frac in rows:
            print(f"    [{lo},{hi}) n={n:6d}  dislocated={frac:5.2f}%")

    plot_comparison(fci_o, psd_o, keVee_o, fci_l, psd_l, keVee_l)


if __name__ == "__main__":
    main()
