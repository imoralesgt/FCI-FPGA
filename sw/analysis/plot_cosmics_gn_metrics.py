"""Full g/n discrimination metrics for the 52.7-hour ambient cosmic-ray CLYC run ("cosmics_0001",
started 2026-09-11, no dedicated source). See docs/log/README.md 8v.

Unlike sw/analysis/plot_optimized_psd_fci.py's DD-generator dataset, this run has no independent
energy-based gamma/neutron labelling: the only real second population is the narrow (n,alpha)t
6Li thermal-neutron capture cluster at ~3,160 keVee (Morales et al., docs/log/README.md
References), a small (~0.2%) fraction of all events riding on a much larger ambient
gamma/cosmic-muon continuum. Every FoM measurement below therefore has to define its own "cluster"
vs. "continuum" classes rather than reusing a fixed source-driven energy band, and the two
independent ways of doing that (a same-energy-window split, and a cumulative-from-LLD split) are
kept as two separate, cross-checking measurements rather than collapsed into one number.

`peak` is the field the GUI's own energy calibration (Energy = c0 + c1*peak + c2*peak^2,
sw/gui/ui/live_view.py) is computed from -- NOT `energy_long` (a 32-sample gate-integral sum,
15-25x larger than `peak` for a typical pulse). Using `energy_long` as if it were the energy axis
was this analysis's own first-draft mistake (docs/log/README.md 8v); every plot here uses `peak`.

Run: sw/.venv/bin/python -m analysis.plot_cosmics_gn_metrics   (from sw/)
"""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np

DATA_PATH = "/home/ivan/datasets/cosmics-CLYC/cosmics_0001_fci_live.csv"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

# settings.json spectrum.calibration = [c0, c1, c2] = [0.0, 0.48, 0.0], applied to `peak` -- the
# same linear calibration the GUI itself used live during this run.
C0_KEVEE, C1_KEVEE_PER_COUNT, C2_KEVEE = 0.0, 0.48, 0.0

# The real analog-stage saturation ceiling sits at ~12,500 raw `peak` counts (~6,000 keVee) --
# below section 8k's theoretical digital-clipping rail, confirmed independently in Excel. ULD is
# set with headroom below that, not at the theoretical rail.
ULD_KEVEE = 5800.0
LLD_INTEGRATED_KEVEE = 100.0
LI6_CAPTURE_KEVEE = 3160.0  # Morales et al. Table 1 calibration point (docs/log/README.md 0, 8n)

# The window used everywhere below to isolate the capture cluster from the *local* continuum at
# the same energy, avoiding the energy-confounding that a fixed, energy-disjoint band would cause.
CLUSTER_WINDOW_KEVEE = (2900.0, 3400.0)

FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))
IQR_TO_SIGMA = 1.349

# tune_fom.py's own established energy-band convention (docs/log/README.md 8j/8v): reused here for
# the FoM-vs-LLD sweep so the two datasets are read off the same convention, with this section's own
# caveat about where it breaks down for a weak/low-purity ambient population.
N_BAND_KEVEE = (2800.0, 3500.0)
G_BAND_UPPER_KEVEE = 2000.0
LLD_SWEEP_KEVEE = [0, 100, 200, 300, 475, 700, 1000, 1500, 1900]

# Iterations requested for the cumulative FoM-vs-Energy sweep: from 100 keVee, roughly doubling, up
# to (but not past) the point where the cluster's own energy range is fully excluded.
FOM_VS_ENERGY_LLD_KEVEE = [100, 250, 500, 1000, 2000, 4000]


def load_dataset(path: str = DATA_PATH):
    """Loads peak/fci/psd, applies the peak-based energy calibration and the ULD cut. Returns
    (peak, fci, psd, keVee), all filtered to keVee <= ULD_KEVEE."""
    lines = open(path).readlines()
    data_lines = [l for l in lines if not l.startswith("#") and not l.startswith("timestamp")]
    rows = list(csv.reader(data_lines))
    peak = np.array([int(r[7]) for r in rows], dtype=np.float64)
    fci = np.array([float(r[3]) for r in rows])
    psd = np.array([float(r[6]) for r in rows])
    n_total = len(rows)

    keVee = C0_KEVEE + C1_KEVEE_PER_COUNT * peak + C2_KEVEE * peak * peak
    keep = keVee <= ULD_KEVEE
    n_discarded = int((~keep).sum())
    print(f"loaded {n_total} events; ULD={ULD_KEVEE:.0f} keVee discards "
          f"{n_discarded} ({100 * n_discarded / n_total:.2f}%), keeping {int(keep.sum())}")
    return peak[keep], fci[keep], psd[keep], keVee[keep]


def robust_stats(a: np.ndarray):
    q = np.percentile(a, [25, 75])
    return float(np.median(a)), float((q[1] - q[0]) / IQR_TO_SIGMA)


def robust_fom(cluster: np.ndarray, continuum: np.ndarray):
    """|median_cluster - median_continuum| / (FWHM_cluster + FWHM_continuum) -- this project's own
    established convention (sw/analysis/tune_fom.py, sw/gui/fom_core.py): the SUM of the two FWHMs,
    not their average."""
    if len(cluster) < 30 or len(continuum) < 30:
        return float("nan")
    mc, sc = robust_stats(cluster)
    mg, sg = robust_stats(continuum)
    denom = FWHM_PER_SIGMA * (sc + sg)
    return abs(mc - mg) / denom if denom > 0 else float("nan")


def _gauss(x, mu, sigma):
    return np.exp(-0.5 * ((x - mu) / sigma) ** 2)


def crossing_point(mu_a: float, s_a: float, mu_b: float, s_b: float) -> float:
    """The value at which two Gaussians (robust mu/sigma, unit-normalized) are equally likely --
    the physically meaningful class boundary, not the statistics-only midpoint of the two medians."""
    x = np.linspace(min(mu_a, mu_b), max(mu_a, mu_b), 8000)
    diff = _gauss(x, mu_a, s_a) - _gauss(x, mu_b, s_b)
    sign_change = np.where(np.diff(np.sign(diff)))[0]
    return float(x[sign_change[0]]) if len(sign_change) else 0.5 * (mu_a + mu_b)


def derive_separation_cuts(fci: np.ndarray, psd: np.ndarray, keVee: np.ndarray):
    """The class-separation cut used everywhere below: the crossing point of the cluster's and the
    *same-energy* continuum's robust Gaussians, both drawn from CLUSTER_WINDOW_KEVEE. This avoids
    two failure modes seen while developing this analysis: (1) using a fixed cut derived from a
    DIFFERENT, contaminated energy band puts the line in the wrong place (an earlier version of the
    vs-Energy plot did this and placed the FCI line at 0.876 instead of ~0.895); (2) a naive
    median-of-two-classes split (rather than the Gaussian crossing point) is a statistics-only
    choice that does not track where the two populations actually become equally likely."""
    win = (keVee >= CLUSTER_WINDOW_KEVEE[0]) & (keVee <= CLUSTER_WINDOW_KEVEE[1])

    def cut_for(vals):
        # First pass: split the window at its own median as a seed (the cluster is the
        # higher-valued side for both FCI and PSD in this dataset).
        seed = np.median(vals[win])
        hi_mu, hi_s = robust_stats(vals[win][vals[win] >= seed])
        lo_mu, lo_s = robust_stats(vals[win][vals[win] < seed])
        return crossing_point(lo_mu, lo_s, hi_mu, hi_s)

    return dict(fci=cut_for(fci), psd=cut_for(psd))


# x-axis ranges kept to the region where g/n discrimination actually happens (the cluster and its
# immediate neighborhood), rather than the full [min, max] range which is dominated by the bulk
# gamma/muon continuum and the low-energy noise-broadened tail neither histogram needs to show.
PSD_HIST_RANGE = (0.75, 0.82)
FCI_HIST_RANGE = (0.85, 0.93)

# The paper's own lower neutron-detection limit (docs/log/README.md 8s, section 6.1 of the paper:
# 517 keVee quenched, 476 keVee after the detector's ~8% resolution) -- this project rounds it to
# 475 keVee throughout (8j/8s/8v). Used here as a second LLD to show the same physical effect the
# paper itself makes: below this limit no real neutron can produce this light output at all, so
# raising the LLD to it should make the (already small) cluster's histogram shoulder relatively more
# prominent by removing gamma/muon continuum that a real neutron could never contribute to.
PAPER_NEUTRON_LIMIT_KEVEE = 475.0


def plot_histograms(fci, psd, keVee, lld: float, suffix: str = ""):
    sel = keVee >= lld
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), dpi=140)
    axes[0].hist(psd[sel], bins=150, range=PSD_HIST_RANGE, color="tab:blue", log=True)
    axes[0].set_xlim(*PSD_HIST_RANGE)
    axes[0].set_xlabel("PSD"); axes[0].set_ylabel("counts (log)")
    axes[0].set_title(f"Energy-integrated PSD histogram\n"
                       f"(LLD={lld:.0f}, ULD={ULD_KEVEE:.0f} keVee, n={sel.sum()})")
    axes[1].hist(fci[sel], bins=150, range=FCI_HIST_RANGE, color="tab:red", log=True)
    axes[1].set_xlim(*FCI_HIST_RANGE)
    axes[1].set_xlabel("FCI"); axes[1].set_ylabel("counts (log)")
    axes[1].set_title(f"Energy-integrated FCI histogram\n"
                       f"(LLD={lld:.0f}, ULD={ULD_KEVEE:.0f} keVee, n={sel.sum()})")
    plt.tight_layout()
    out = OUT_DIR / f"cosmics_psd_fci_histograms{suffix}.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_vs_energy_full(fci, psd, keVee):
    fig, axes = plt.subplots(2, 1, figsize=(11, 9), dpi=140)
    for ax, Y, name, ylim in [(axes[0], fci, "FCI", (0.5, 0.95)), (axes[1], psd, "PSD", (0.5, 1.15))]:
        h = ax.hist2d(keVee, Y, bins=[280, 220], range=[[0, 6800], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm())
        ax.set_xlabel("Energy (keVee, from peak)"); ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy -- cosmics_0001, full range showing both features")
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvline(LI6_CAPTURE_KEVEE, color="red", linestyle="--", linewidth=1, alpha=0.6)
        ax.axvline(12500 * C1_KEVEE_PER_COUNT, color="orange", linestyle="--", linewidth=1, alpha=0.6)
    axes[0].text(LI6_CAPTURE_KEVEE, 0.93, " 6Li capture\n (~3150 keVee)", color="red", fontsize=8, va="top")
    axes[0].text(12500 * C1_KEVEE_PER_COUNT, 0.93, " ADC/analog\n saturation\n (~6000 keVee)",
                 color="orange", fontsize=8, va="top", ha="right")
    plt.tight_layout()
    out = OUT_DIR / "cosmics_fci_psd_vs_energy.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_vs_energy_zoom(fci, psd, keVee):
    lo, hi = 1500.0, 4500.0
    mask = (keVee > lo) & (keVee < hi)
    E, F, P = keVee[mask], fci[mask], psd[mask]
    fig, axes = plt.subplots(2, 1, figsize=(11, 9), dpi=140)
    for ax, Y, name, ylim in [(axes[0], F, "FCI", (0.83, 0.94)), (axes[1], P, "PSD", (0.70, 0.86))]:
        h = ax.hist2d(E, Y, bins=[300, 150], range=[[lo, hi], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm(vmin=1, vmax=300))
        ax.set_xlabel("Energy (keVee, from peak)"); ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy, zoomed on the capture-cluster region")
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvline(LI6_CAPTURE_KEVEE, color="red", linestyle="--", linewidth=1, alpha=0.7)
    plt.tight_layout()
    out = OUT_DIR / "cosmics_6li_cluster_zoom.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_vs_energy_shaded(fci, psd, keVee, sep_fci: float, sep_psd: float):
    plot_lo = 80.0
    fig, axes = plt.subplots(2, 1, figsize=(11, 10), dpi=140)
    for ax, Y, name, sep, ylim in [(axes[0], psd, "PSD", sep_psd, (0.5, 1.15)),
                                    (axes[1], fci, "FCI", sep_fci, (0.45, 0.95))]:
        logE = np.log10(np.clip(keVee, plot_lo, None))
        h = ax.hist2d(logE, Y, bins=[260, 220], range=[[np.log10(plot_lo), np.log10(ULD_KEVEE * 1.15)], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm())
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        for lld in LLD_SWEEP_KEVEE[1:]:
            lld_line = ax.axvline(np.log10(lld), color="black", linestyle=":", linewidth=1.0)
            lld_line.set_path_effects([pe.Stroke(linewidth=2.2, foreground="white"), pe.Normal()])
        ax.axvline(np.log10(LI6_CAPTURE_KEVEE), color="magenta", linestyle="-.", linewidth=1.4,
                   label=f"6Li capture, {LI6_CAPTURE_KEVEE:.0f} keVee")
        ax.axvline(np.log10(ULD_KEVEE), color="red", linestyle="--", linewidth=1.4,
                   label=f"ULD, {ULD_KEVEE:.0f} keVee")
        sep_line = ax.axhline(sep, color="black", linestyle="--", linewidth=1.5,
                               label=f"class separation ({sep:.3f})")
        sep_line.set_path_effects([pe.Stroke(linewidth=3.5, foreground="white"), pe.Normal()])
        ticks = [100, 200, 500, 1000, 2000, 5000]
        ax.set_xticks([np.log10(t) for t in ticks]); ax.set_xticklabels([str(t) for t in ticks])
        ax.set_xlim(np.log10(plot_lo), np.log10(ULD_KEVEE * 1.15))
        ax.set_xlabel("Energy (keVee)  -- dotted lines mark cumulative LLD values"); ax.set_ylabel(name)
        ax.set_title(f"cosmics_0001: {name} vs Energy, cumulative LLD markers, ULD applied")
        ax.legend(loc="lower right" if name == "PSD" else "upper left", fontsize=8, framealpha=0.9)
    plt.tight_layout()
    out = OUT_DIR / "cosmics_psd_fci_vs_energy_shaded.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_fom_vs_lld(fci, psd, keVee):
    """tune_fom.py's own fixed-band convention: a fixed neutron-like band and a gamma-like band
    whose lower edge is the swept LLD. Included for continuity with every other FoM-vs-LLD plot
    this log has produced (docs/log/README.md 8j) -- see 8v's own caveat about why this particular
    band inverts (decreases with LLD) on this dataset's weak, low-purity ambient population."""
    n_mask = (keVee >= N_BAND_KEVEE[0]) & (keVee <= N_BAND_KEVEE[1])
    results = {"psd": [], "fci": []}
    for lld in LLD_SWEEP_KEVEE:
        g_mask = (keVee >= lld) & (keVee < G_BAND_UPPER_KEVEE)
        results["psd"].append(robust_fom(psd[n_mask], psd[g_mask]))
        results["fci"].append(robust_fom(fci[n_mask], fci[g_mask]))

    fig, ax = plt.subplots(figsize=(8, 5.5), dpi=140)
    ax.plot(LLD_SWEEP_KEVEE, results["psd"], "o-", color="tab:blue", label="PSD")
    ax.plot(LLD_SWEEP_KEVEE, results["fci"], "s-", color="tab:red", label="FCI")
    ax.set_xlabel("Lower energy cut, ENERGY > X (keVee)  [gamma band upper edge fixed at 2000]")
    ax.set_ylabel(r"FoM = $|median_n-median_g|$ / (FWHM$_n$ + FWHM$_g$)")
    ax.set_title(f"cosmics_0001: FoM vs cumulative LLD cut\n"
                 f"(neutron band {N_BAND_KEVEE[0]:.0f}-{N_BAND_KEVEE[1]:.0f} keVee, "
                 f"ULD={ULD_KEVEE:.0f} keVee)")
    ax.legend(); ax.grid(alpha=0.3)
    plt.tight_layout()
    out = OUT_DIR / "cosmics_fom_vs_lld.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")
    return results


def plot_fom_vs_energy(fci, psd, keVee, sep_fci: float, sep_psd: float,
                        out_name: str = "cosmics_fom_vs_energy.png"):
    """FoM vs Energy, as clarified: at each swept lower-energy cut E, the population is the
    CUMULATIVE tail [E, ULD] (not a local window) -- "integrating the events marked with the x-axis
    Energy value all the way up to the ULD". Since a cumulative tail has no independent energy-band
    labelling, the two classes are the SAME validated cluster/continuum cut used everywhere else in
    this section (sep_fci/sep_psd from derive_separation_cuts), applied to whichever events fall in
    that tail.

    This produces a genuinely different curve from plot_fom_vs_lld's fixed-band sweep, and for a
    revealing reason once you look at the class populations rather than only the final FoM number:
    FCI's cluster-like population (metric >= sep_fci) is essentially CONSTANT (~690 events, median
    ~0.903) all the way from LLD=100 to LLD=2000 -- the fixed FCI cut never picks up meaningful
    numbers of low- or mid-energy continuum events, because FCI only approaches sep_fci near the
    real capture peak's own energy. What DOES change with LLD is the continuum side: its own median
    rises and its own spread (IQR-based sigma) shrinks sharply as the wide, low-energy,
    noise-broadened continuum tail (the same low-energy PSD/FCI broadening documented in 8d/8v's own
    histograms) is progressively excluded from the comparison. The combined-FWHM denominator falls
    faster than the median gap does, so FoM rises with LLD even though the cluster's own population
    barely moves -- converging, by LLD=2000, on almost exactly the independently-derived
    same-energy-window cluster FoM (1.135 FCI / 0.951 PSD) reported earlier in this section. At
    LLD=4000 the cluster's own energy range (2,900-3,400 keVee) is fully excluded from the
    population, so the "cluster-like" side collapses to a handful of non-physical high-tail events
    and the FoM is reported as undefined (n < 30) rather than as a misleading number."""
    results = {"psd": [], "fci": []}
    sep = {"fci": sep_fci, "psd": sep_psd}
    diagnostics = []
    for lld in FOM_VS_ENERGY_LLD_KEVEE:
        pop_mask = (keVee >= lld) & (keVee <= ULD_KEVEE)
        row = {"lld": lld, "n_pop": int(pop_mask.sum())}
        for name, vals in [("fci", fci), ("psd", psd)]:
            v = vals[pop_mask]
            cut = sep[name]
            cluster, continuum = v[v >= cut], v[v < cut]
            row[f"n_cluster_{name}"] = len(cluster)
            row[f"n_continuum_{name}"] = len(continuum)
            fom = robust_fom(cluster, continuum)
            results[name].append(fom)
            row[f"fom_{name}"] = fom
        diagnostics.append(row)

    print(f"\n{'LLD':>6} {'n_pop':>8} {'n_cont_psd':>11} {'n_clu_psd':>10} "
          f"{'n_cont_fci':>11} {'n_clu_fci':>10} {'PSD FoM':>9} {'FCI FoM':>9}")
    for r in diagnostics:
        print(f"{r['lld']:>6} {r['n_pop']:>8} {r['n_continuum_psd']:>11} {r['n_cluster_psd']:>10} "
              f"{r['n_continuum_fci']:>11} {r['n_cluster_fci']:>10} "
              f"{r['fom_psd']:>9.3f} {r['fom_fci']:>9.3f}")

    fig, ax = plt.subplots(figsize=(8, 5.5), dpi=140)
    ax.plot(FOM_VS_ENERGY_LLD_KEVEE, results["psd"], "o-", color="tab:blue", label="PSD")
    ax.plot(FOM_VS_ENERGY_LLD_KEVEE, results["fci"], "s-", color="tab:red", label="FCI")
    ax.set_xscale("log")
    ax.set_xticks(FOM_VS_ENERGY_LLD_KEVEE)
    ax.set_xticklabels([str(v) for v in FOM_VS_ENERGY_LLD_KEVEE])
    ax.set_xlabel(f"Energy (keVee) -- cumulative lower cut, integrated up to ULD={ULD_KEVEE:.0f} keVee")
    ax.set_ylabel(r"FoM = $|median_{cluster}-median_{cont}|$ / (FWHM$_{cluster}$+FWHM$_{cont}$)")
    ax.set_title("cosmics_0001: FoM vs Energy\n"
                 "(classes = fixed cluster/continuum cut, population = [Energy, ULD])")
    ax.legend(); ax.grid(alpha=0.3)
    plt.tight_layout()
    out = OUT_DIR / out_name
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")
    return results, diagnostics


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    peak, fci, psd, keVee = load_dataset()

    sep = derive_separation_cuts(fci, psd, keVee)
    print(f"class-separation cuts (same-energy-window crossing point): "
          f"FCI={sep['fci']:.4f}  PSD={sep['psd']:.4f}")

    plot_histograms(fci, psd, keVee, LLD_INTEGRATED_KEVEE)
    plot_histograms(fci, psd, keVee, PAPER_NEUTRON_LIMIT_KEVEE, suffix="_lld475")
    plot_vs_energy_full(fci, psd, keVee)
    plot_vs_energy_zoom(fci, psd, keVee)
    plot_vs_energy_shaded(fci, psd, keVee, sep["fci"], sep["psd"])
    plot_fom_vs_lld(fci, psd, keVee)
    plot_fom_vs_energy(fci, psd, keVee, sep["fci"], sep["psd"])


if __name__ == "__main__":
    main()
