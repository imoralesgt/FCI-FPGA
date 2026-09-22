"""Full g/n discrimination metrics for the LIVE, on-hardware "cosmics_psd_fci_optimized" run
(started 2026-09-21 17:09:42 on nsil-red), which is the actual hardware confirmation
sw/analysis/sweep_cosmics_gates.py's offline grid search and sw/analysis/plot_cosmics_optimal_psd_fci.py's
raw-trace recomputation both flagged as still missing (docs/log/README.md 8v). Unlike those two
scripts, PSD and FCI here are read directly from the LIST CSV -- computed by the real
`dual_gate_integrator`/`bin_accumulator` hardware with the grid-search-optimal configuration loaded,
confirmed from the log's own header, not modeled in software:

    psd: short_gate=5, long_gate=47            (grid-search optimum)
    fci: psa_l_lo=2, psa_l_hi=60, psa_w_lo=2, psa_w_hi=178   (grid-search optimum)

Reuses sw/analysis/plot_cosmics_gn_metrics.py's and sw/analysis/plot_optimized_psd_fci.py's
established building blocks (same crossing-point separation-cut derivation, same cumulative
FoM-vs-Energy sweep, same pooled double-Gaussian fit) so these numbers are computed exactly the same
way as the deployed-config (8v) and offline-optimal (this script's own sibling) ones -- only the
input data source differs: real hardware output, not a recorded-and-replayed configuration.

This file is snapshotted from a run that may still be recording when read (see the
remote-dev-machine project memory) -- event counts and duration below reflect whatever was captured
at snapshot time, not a finished run.

Run: sw/.venv/bin/python -m analysis.plot_cosmics_live_optimal   (from sw/)
"""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors
import matplotlib.pyplot as plt
import numpy as np

from analysis.plot_cosmics_gn_metrics import (
    CLUSTER_WINDOW_KEVEE,
    PAPER_NEUTRON_LIMIT_KEVEE,
    ULD_KEVEE,
    derive_separation_cuts,
    plot_fom_vs_energy,
)
from analysis.plot_optimized_psd_fci import fit_double_gaussian, hist_panel

DATA_PATH = "/home/ivan/datasets/cosmics-CLYC/cosmics_psd_fci_optimized_0001_fci_live.csv"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

C1_KEVEE_PER_COUNT = 0.48  # settings.json spectrum.calibration, applied to `peak`, same as 8v

PSD_LABEL = "PSD (live, optimal: short=5, long=47)"
FCI_LABEL = "FCI (live, optimal: lo=2, l_hi=60, w_hi=178)"

# Fixed y-axis ranges for the full-spectrum vs-Energy plot -- identical values to
# sw/analysis/plot_cosmics_optimal_psd_fci.py's own PSD_VS_ENERGY_RANGE/FCI_VS_ENERGY_RANGE, so
# every full-range PSD/FCI-vs-Energy plot for this configuration (live hardware or offline
# raw-trace recompute) shares one scale rather than each auto-ranging to its own data. Not used for
# the zoomed cluster view or the energy-integrated histograms.
PSD_VS_ENERGY_RANGE = (0.9, 1.025)
FCI_VS_ENERGY_RANGE = (0.85, 0.97)


def load_dataset(path: str = DATA_PATH):
    lines = open(path).readlines()
    header = [l for l in lines if l.startswith("#")]
    for l in header:
        print(l.rstrip())
    data_lines = [l for l in lines if not l.startswith("#") and not l.startswith("timestamp")]
    rows = list(csv.reader(data_lines))
    ts = np.array([int(r[0]) for r in rows], dtype=np.float64)
    peak = np.array([int(r[7]) for r in rows], dtype=np.float64)
    fci = np.array([float(r[3]) for r in rows])
    psd = np.array([float(r[6]) for r in rows])
    n_total = len(rows)

    duration_s = (ts[-1] - ts[0]) / 50e6  # 50 MHz hardware timestamp counter
    print(f"\nn events: {n_total}, duration so far: {duration_s / 3600:.2f} h, "
          f"mean rate: {n_total / duration_s:.3f} evt/s")

    keVee = C1_KEVEE_PER_COUNT * peak
    keep = (keVee <= ULD_KEVEE) & np.isfinite(fci) & np.isfinite(psd)
    n_discarded = int((~keep).sum())
    print(f"ULD={ULD_KEVEE:.0f} keVee discards {n_discarded} ({100 * n_discarded / n_total:.2f}%), "
          f"keeping {int(keep.sum())}")
    return fci[keep], psd[keep], keVee[keep]


def _padded_range(v: np.ndarray, cluster_v: np.ndarray | None = None,
                   lo_pct=0.5, hi_pct=99.5, pad_frac=0.08):
    """See sw/analysis/plot_cosmics_optimal_psd_fci.py's own version of this function for why a
    bare percentile crop is unsafe here: the cluster is a small fraction of events and can sit
    outside a naive percentile window, clipping the real signal these plots exist to show."""
    lo, hi = np.percentile(v, [lo_pct, hi_pct])
    if cluster_v is not None and len(cluster_v):
        lo = min(lo, cluster_v.min())
        hi = max(hi, cluster_v.max())
    pad = (hi - lo) * pad_frac
    return lo - pad, hi + pad


def plot_vs_energy(fci, psd, keVee, sep_fci, sep_psd, psd_range, fci_range):
    fig, axes = plt.subplots(2, 1, figsize=(11, 9), dpi=140)
    for ax, Y, name, ylim, sep in [(axes[0], psd, PSD_LABEL, psd_range, sep_psd),
                                    (axes[1], fci, FCI_LABEL, fci_range, sep_fci)]:
        h = ax.hist2d(keVee, Y, bins=[280, 220], range=[[0, ULD_KEVEE * 1.05], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm())
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvline(3160, color="red", linestyle="--", linewidth=1, alpha=0.7,
                   label="6Li capture, 3160 keVee")
        ax.axhline(sep, color="black", linestyle="--", linewidth=1.2,
                   label=f"class separation ({sep:.4f})")
        ax.set_xlabel("Energy (keVee, peak)"); ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy -- cosmics_psd_fci_optimized, LIVE hardware")
        ax.legend(fontsize=8, loc="upper right")
    plt.tight_layout()
    out = OUT_DIR / "cosmics_live_optimal_psd_fci_vs_energy.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_vs_energy_zoom(fci, psd, keVee):
    lo, hi = 2000.0, 4200.0
    mask = (keVee > lo) & (keVee < hi)
    E, F, P = keVee[mask], fci[mask], psd[mask]
    cluster = (keVee >= CLUSTER_WINDOW_KEVEE[0]) & (keVee <= CLUSTER_WINDOW_KEVEE[1])
    fig, axes = plt.subplots(2, 1, figsize=(11, 9), dpi=140)
    for ax, Y, Yc, name in [(axes[0], F, fci[cluster], FCI_LABEL), (axes[1], P, psd[cluster], PSD_LABEL)]:
        ylim = _padded_range(Y, cluster_v=Yc, lo_pct=1.0, hi_pct=99.5, pad_frac=0.15)
        h = ax.hist2d(E, Y, bins=[280, 180], range=[[lo, hi], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm(vmin=1, vmax=300))
        ax.set_xlabel("Energy (keVee, peak)"); ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy, zoomed on the capture-cluster region -- LIVE hardware")
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvline(3160, color="red", linestyle="--", linewidth=1, alpha=0.7)
    plt.tight_layout()
    out = OUT_DIR / "cosmics_live_optimal_6li_cluster_zoom.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_histograms(fci, psd, keVee, psd_range, fci_range):
    sel = keVee >= 100.0
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), dpi=140)
    axes[0].hist(psd[sel], bins=150, range=psd_range, color="tab:blue", log=True)
    axes[0].set_xlim(*psd_range)
    axes[0].set_xlabel(PSD_LABEL); axes[0].set_ylabel("counts (log)")
    axes[0].set_title(f"Energy-integrated PSD histogram, LIVE hardware\n(n={sel.sum()})")
    axes[1].hist(fci[sel], bins=150, range=fci_range, color="tab:red", log=True)
    axes[1].set_xlim(*fci_range)
    axes[1].set_xlabel(FCI_LABEL); axes[1].set_ylabel("counts (log)")
    axes[1].set_title(f"Energy-integrated FCI histogram, LIVE hardware\n(n={sel.sum()})")
    plt.tight_layout()
    out = OUT_DIR / "cosmics_live_optimal_psd_fci_histograms.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_fom_at_lld(values, keVee, cut, name, out_name, color,
                     lld: float, uld: float = ULD_KEVEE):
    """Same procedure as sw/analysis/plot_cosmics_optimal_psd_fci.py's plot_fom_at_lld: ONE pooled
    double-Gaussian fit (fit_double_gaussian) to [LLD, ULD], `cut` only seeding/labelling, and the
    separation line pinned to the same crossing-point cut used throughout this section rather than
    to whatever this individual fit's own crossing point comes out to."""
    m = (keVee >= lld) & (keVee <= uld)
    v = values[m]
    cluster_seed, continuum_seed = v[v >= cut], v[v < cut]
    fit = fit_double_gaussian(continuum_seed, cluster_seed)
    fit["crossing"] = cut
    fig, ax = plt.subplots(figsize=(8, 5.5), dpi=140)
    hist_panel(ax, fit,
               xlabel=name,
               title=f"cosmics_psd_fci_optimized (LIVE): {name} FoM at LLD={lld:.0f} keVee, "
                     f"ULD={uld:.0f} keVee\ndouble-Gaussian fit, FoM = {fit['fom']:.3f}")
    plt.tight_layout()
    out = OUT_DIR / out_name
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}  FoM={fit['fom']:.3f}  n_cluster_seed={len(cluster_seed)}  "
          f"n_continuum_seed={len(continuum_seed)}")
    return fit["fom"]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fci, psd, keVee = load_dataset()

    sep = derive_separation_cuts(fci, psd, keVee)
    print(f"class-separation cuts (same-energy-window crossing point, LIVE optimal): "
          f"FCI={sep['fci']:.4f}  PSD={sep['psd']:.4f}")

    cluster = (keVee >= CLUSTER_WINDOW_KEVEE[0]) & (keVee <= CLUSTER_WINDOW_KEVEE[1])
    print(f"n in cluster window {CLUSTER_WINDOW_KEVEE}: {cluster.sum()}")
    psd_range = _padded_range(psd, cluster_v=psd[cluster])
    fci_range = _padded_range(fci, cluster_v=fci[cluster])
    print(f"auto-ranged axes (cluster-inclusive): PSD={psd_range}  FCI={fci_range}")

    plot_vs_energy(fci, psd, keVee, sep["fci"], sep["psd"],
                    PSD_VS_ENERGY_RANGE, FCI_VS_ENERGY_RANGE)
    plot_vs_energy_zoom(fci, psd, keVee)
    plot_histograms(fci, psd, keVee, psd_range, fci_range)

    plot_fom_vs_energy(fci, psd, keVee, sep["fci"], sep["psd"],
                        out_name="cosmics_live_optimal_fom_vs_energy.png")

    for lld, suffix in [(PAPER_NEUTRON_LIMIT_KEVEE, "475"), (1000.0, "1000"), (2000.0, "2000")]:
        plot_fom_at_lld(psd, keVee, sep["psd"], PSD_LABEL,
                         f"cosmics_live_optimal_psd_fom_lld{suffix}.png", "tab:blue", lld=lld)
        plot_fom_at_lld(fci, keVee, sep["fci"], FCI_LABEL,
                         f"cosmics_live_optimal_fci_fom_lld{suffix}.png", "tab:red", lld=lld)


if __name__ == "__main__":
    main()
