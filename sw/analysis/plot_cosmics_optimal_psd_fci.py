"""PSD/FCI vs Energy, energy-integrated histograms, and FoM vs Energy for the cosmics_0001 dataset
RECOMPUTED with the grid-search-optimal PSD gates and FCI window found in
sw/analysis/sweep_cosmics_gates.py (docs/log/README.md 8v):

    PSD: short_gate=5, long_gate=47      (deployed was short_gate=10, long_gate=32)
    FCI: lo=2, l_hi=60, w_hi=178         (deployed was lo=2, l_hi=38, w_hi=120)

The input CSV (host_timestamp, energy_kevee, psd_opt, fci_opt) is produced by running
sw/analysis/compute_cosmics_optimal_psd_fci.py on nsil-red, against the FULL raw scope-trace file
(not just the [2,000, 4,000] keVee slice sweep_cosmics_gates.py searches on -- these plots need the
whole spectrum), then copying the small result back here. It applies the exact same hardware model
(dual_gate_integrator/bin_accumulator's equations) to every captured raw trace "as if it was done in
the FPGA" with the new parameters -- this dataset never actually ran with this configuration itself,
so this script's own numbers are a software recomputation, not hardware output (see 8v's own caveat:
an offline sweep result should not be treated as verified until checked against real events -- by
now it has been, in sw/analysis/plot_cosmics_live_optimal.py's live-hardware run of this same
configuration, which is why that script's dataset is loaded here too, purely to set matching y-axis
ranges so the two sets of plots are directly comparable; see plot_vs_energy).

Reuses sw/analysis/plot_cosmics_gn_metrics.py's robust-statistics building blocks (same FoM
convention, same crossing-point separation-cut derivation, same cumulative FoM-vs-Energy sweep)
so the optimal-config numbers are computed exactly the same way as the deployed-config ones
throughout 8v -- only the input psd/fci VALUES differ.

Run: sw/.venv/bin/python -m analysis.plot_cosmics_optimal_psd_fci   (from sw/)
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
    PAPER_NEUTRON_LIMIT_KEVEE,
    ULD_KEVEE,
    derive_separation_cuts,
    plot_fom_vs_energy,
)
from analysis.plot_cosmics_live_optimal import load_dataset as load_live_dataset
from analysis.plot_optimized_psd_fci import fit_double_gaussian, hist_panel

DATA_PATH = "/home/ivan/datasets/cosmics-CLYC/cosmics_0001_optimal_psd_fci.csv"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

PSD_LABEL = "PSD (optimal: short=5, long=47)"
FCI_LABEL = "FCI (optimal: lo=2, l_hi=60, w_hi=178)"

# Fixed y-axis ranges for the full-spectrum vs-Energy plots specifically (plot_vs_energy) -- shared
# verbatim with sw/analysis/plot_cosmics_live_optimal.py's own plot_vs_energy, so every full-range
# PSD/FCI-vs-Energy plot for this configuration (offline raw-trace recompute or live hardware) uses
# the same scale and is directly comparable at a glance, not just when the auto-ranged extents
# happen to coincide. Covers both datasets' own full extents (checked directly) with headroom;
# NOT used for the zoomed cluster view or the energy-integrated histograms, which auto-range to
# their own, different purposes.
PSD_VS_ENERGY_RANGE = (0.9, 1.025)
FCI_VS_ENERGY_RANGE = (0.85, 0.97)


def load_dataset(path: str = DATA_PATH):
    rows = list(csv.reader(l for l in open(path) if not l.startswith("host_timestamp")))
    keVee = np.array([float(r[1]) for r in rows])
    psd = np.array([float(r[2]) for r in rows])
    fci = np.array([float(r[3]) for r in rows])
    n_total = len(rows)
    keep = np.isfinite(keVee) & np.isfinite(psd) & np.isfinite(fci) & (keVee <= ULD_KEVEE) & (keVee > 0)
    n_discarded = int((~keep).sum())
    print(f"loaded {n_total} events; ULD={ULD_KEVEE:.0f} keVee / non-finite discards "
          f"{n_discarded} ({100 * n_discarded / n_total:.2f}%), keeping {int(keep.sum())}")
    return fci[keep], psd[keep], keVee[keep]


def _padded_range(v: np.ndarray, cluster_v: np.ndarray | None = None,
                   lo_pct=0.5, hi_pct=99.5, pad_frac=0.08):
    """A plain percentile crop is NOT safe here: the cluster is a tiny (~0.2%) fraction of events,
    and for FCI specifically it sits almost entirely ABOVE the 99.5th percentile of the whole
    distribution (checked directly: cluster range 0.941-0.960, p99.5 of everything is only 0.921) --
    so a bare percentile-based range clips the real signal this plot exists to show, not just an
    uninteresting outlier tail. When `cluster_v` (the same metric's values within the validated
    2,900-3,400 keVee window) is given, the range is widened as needed to fully contain it."""
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
        ax.set_xlabel("Energy (keVee, recomputed peak amplitude)"); ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy -- cosmics_0001, offline-optimal config")
        ax.legend(fontsize=8, loc="upper right")
    plt.tight_layout()
    out = OUT_DIR / "cosmics_optimal_psd_fci_vs_energy.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_vs_energy_zoom(fci, psd, keVee):
    """The full-range plot (plot_vs_energy) can make a real cluster invisible: it is a small
    fraction of events sitting close to the local continuum trend, and a y-axis spanning the whole
    saturating curve compresses that gap to a sliver of the panel -- checked directly, FCI's cluster
    here sits only ~0.034 above the continuum (an 8.6-sigma separation using the continuum's own
    IQR-based sigma, not a small effect) but that is under 10% of the full-range panel's y-span.
    Zooming to the cluster's own energy neighborhood, the same way 8v's plot_vs_energy_zoom does for
    the deployed configuration, is what actually shows it."""
    lo, hi = 2000.0, 4200.0
    mask = (keVee > lo) & (keVee < hi)
    E, F, P = keVee[mask], fci[mask], psd[mask]
    cluster = (keVee >= 2900.0) & (keVee <= 3400.0)
    fig, axes = plt.subplots(2, 1, figsize=(11, 9), dpi=140)
    for ax, Y, Yc, name in [(axes[0], F, fci[cluster], FCI_LABEL), (axes[1], P, psd[cluster], PSD_LABEL)]:
        ylim = _padded_range(Y, cluster_v=Yc, lo_pct=1.0, hi_pct=99.5, pad_frac=0.15)
        h = ax.hist2d(E, Y, bins=[280, 180], range=[[lo, hi], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm(vmin=1, vmax=300))
        ax.set_xlabel("Energy (keVee, recomputed peak amplitude)"); ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy, zoomed on the capture-cluster region -- offline-optimal config")
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvline(3160, color="red", linestyle="--", linewidth=1, alpha=0.7)
    plt.tight_layout()
    out = OUT_DIR / "cosmics_optimal_6li_cluster_zoom.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_histograms(fci, psd, keVee, psd_range, fci_range):
    sel = keVee >= 100.0
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), dpi=140)
    axes[0].hist(psd[sel], bins=150, range=psd_range, color="tab:blue", log=True)
    axes[0].set_xlim(*psd_range)
    axes[0].set_xlabel(PSD_LABEL); axes[0].set_ylabel("counts (log)")
    axes[0].set_title(f"Energy-integrated PSD histogram, offline-optimal config\n(n={sel.sum()})")
    axes[1].hist(fci[sel], bins=150, range=fci_range, color="tab:red", log=True)
    axes[1].set_xlim(*fci_range)
    axes[1].set_xlabel(FCI_LABEL); axes[1].set_ylabel("counts (log)")
    axes[1].set_title(f"Energy-integrated FCI histogram, offline-optimal config\n(n={sel.sum()})")
    plt.tight_layout()
    out = OUT_DIR / "cosmics_optimal_psd_fci_histograms.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_fom_at_lld(values, keVee, cut, name, out_name, color,
                     lld: float = PAPER_NEUTRON_LIMIT_KEVEE, uld: float = ULD_KEVEE):
    """A single, well-quantified FoM at a fixed LLD, instead of the cumulative FoM-vs-Energy sweep's
    own lowest points. Below the paper's own neutron-detection limit (docs/log/README.md 8v: 517
    keVee quenched, 476 after the detector's resolution, this project rounds to 475), the population
    is dominated by the strongly skewed, low-energy noise-broadened tail (same broadening documented
    in 8d and visible in every energy-integrated histogram in this section) -- a real physical
    population, but one that inflates the continuum group's own spread so much that the resulting
    FoM depends more on exactly how much of that skew survives whatever cut is applied than on the
    actual cluster/continuum separation. Fixing the floor at the paper's own limit is the same
    argument this section already made for the LLD=475 keVee histogram pair (8v): below it, no real
    neutron can produce this light output at all, so the population above it is a fair one to score.

    The lobes at LLD=475 are not Gaussian-like (checked visually), so the FoM here comes from a
    proper double-Gaussian fit -- sw/analysis/plot_optimized_psd_fci.py's own established
    `fit_double_gaussian`, ONE six-parameter fit to the POOLED [LLD, ULD] histogram (not two
    independently-fitted single Gaussians): the `cut` derived elsewhere in this section only seeds
    the initial guess and labels which fitted component is which, exactly as that function already
    does for this project's other double-Gaussian fits (§8s).
    """
    m = (keVee >= lld) & (keVee <= uld)
    v = values[m]
    cluster_seed, continuum_seed = v[v >= cut], v[v < cut]
    fit = fit_double_gaussian(continuum_seed, cluster_seed)
    # hist_panel draws fit["crossing"] as the separation line, which fit_double_gaussian computes
    # fresh from ITS OWN fitted components -- a different number, in general, from `cut` (the
    # same-energy-window crossing point this section uses everywhere else: the vs-Energy plots, the
    # FoM-vs-Energy sweep, the grid-search histograms). Overriding it here keeps one single
    # separation value per metric across the whole section instead of a plot-specific refit of it;
    # the FoM itself is unaffected (it comes from the fitted components' own mu/sigma, not from
    # wherever the line is drawn).
    fit["crossing"] = cut
    fig, ax = plt.subplots(figsize=(8, 5.5), dpi=140)
    hist_panel(ax, fit,
               xlabel=name,
               title=f"cosmics_0001: {name} FoM at LLD={lld:.0f} keVee, ULD={uld:.0f} keVee\n"
                     f"double-Gaussian fit, FoM = {fit['fom']:.3f}")
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
    print(f"class-separation cuts (same-energy-window crossing point, optimal config): "
          f"FCI={sep['fci']:.4f}  PSD={sep['psd']:.4f}")

    cluster = (keVee >= 2900.0) & (keVee <= 3400.0)
    psd_range = _padded_range(psd, cluster_v=psd[cluster])
    fci_range = _padded_range(fci, cluster_v=fci[cluster])
    print(f"auto-ranged axes (cluster-inclusive), this dataset alone: PSD={psd_range}  "
          f"FCI={fci_range}")

    # Shared with sw/analysis/plot_cosmics_live_optimal.py's own vs-Energy/histogram plots, so the
    # offline raw-trace recomputation and the live hardware run (same grid-search-optimal
    # configuration) are directly, visually comparable on the same y-axis -- the union of both
    # datasets' own cluster-inclusive ranges, so neither plot's axis clips a feature only the OTHER
    # dataset's own range would have caught.
    live_fci, live_psd, live_keVee = load_live_dataset()
    live_cluster = (live_keVee >= 2900.0) & (live_keVee <= 3400.0)
    live_psd_range = _padded_range(live_psd, cluster_v=live_psd[live_cluster])
    live_fci_range = _padded_range(live_fci, cluster_v=live_fci[live_cluster])
    psd_range = (min(psd_range[0], live_psd_range[0]), max(psd_range[1], live_psd_range[1]))
    fci_range = (min(fci_range[0], live_fci_range[0]), max(fci_range[1], live_fci_range[1]))
    print(f"shared axes (union with live-hardware dataset): PSD={psd_range}  FCI={fci_range}")

    plot_vs_energy(fci, psd, keVee, sep["fci"], sep["psd"],
                    PSD_VS_ENERGY_RANGE, FCI_VS_ENERGY_RANGE)
    plot_vs_energy_zoom(fci, psd, keVee)
    plot_histograms(fci, psd, keVee, psd_range, fci_range)

    plot_fom_vs_energy(fci, psd, keVee, sep["fci"], sep["psd"],
                        out_name="cosmics_optimal_fom_vs_energy.png")

    plot_fom_at_lld(psd, keVee, sep["psd"], PSD_LABEL,
                     "cosmics_optimal_psd_fom_lld475.png", "tab:blue", lld=PAPER_NEUTRON_LIMIT_KEVEE)
    plot_fom_at_lld(fci, keVee, sep["fci"], FCI_LABEL,
                     "cosmics_optimal_fci_fom_lld475.png", "tab:red", lld=PAPER_NEUTRON_LIMIT_KEVEE)
    plot_fom_at_lld(psd, keVee, sep["psd"], PSD_LABEL,
                     "cosmics_optimal_psd_fom_lld1000.png", "tab:blue", lld=1000.0)
    plot_fom_at_lld(fci, keVee, sep["fci"], FCI_LABEL,
                     "cosmics_optimal_fci_fom_lld1000.png", "tab:red", lld=1000.0)
    plot_fom_at_lld(psd, keVee, sep["psd"], PSD_LABEL,
                     "cosmics_optimal_psd_fom_lld2000.png", "tab:blue", lld=2000.0)
    plot_fom_at_lld(fci, keVee, sep["fci"], FCI_LABEL,
                     "cosmics_optimal_fci_fom_lld2000.png", "tab:red", lld=2000.0)


if __name__ == "__main__":
    main()
