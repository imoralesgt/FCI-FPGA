"""Raw-trace grid search for the PSD gate widths and FCI window bounds that best separate the
6Li(n,alpha)t thermal-neutron capture cluster from the surrounding continuum in the cosmics_0001
ambient dataset (docs/log/README.md 8v), restricted to a fixed LLD=2,000 / ULD=4,000 keVee band
close to the cluster (at 3,160 keVee) rather than the whole spectrum.

Uses sw/analysis/fpga_model.py's exact hardware model (bin_accumulator's ASDM/FCI ratio,
dual_gate_integrator's PSD ratio) applied to raw scope traces -- an offline sweep over
configurations the hardware never actually ran with, not the single fixed window the live
acquisition happened to be using.

FoM scoring here is deliberately NOT sw/analysis/tune_fom.py's double-Gaussian fit, nor its fixed
asymmetric energy bands: the user reported that offline sweep "did not work at all: it was an
artifact" (project memory tune-fom-offline-sweep-artifact), and this project's own analysis of THIS
dataset (8v) already found tune_fom.py's fixed bands (2,800-3,500 "neutron-like" / [LLD,2,000)
"gamma-like") break down on a weak, low-purity ambient population.

**This objective went through two wrong versions before landing on the one below -- both are kept
here because the failure modes are instructive, not just as history.**

*v1*: score "cluster" (2,900-3,400 keVee) against "continuum" (the rest of [2,000, 4,000] keVee)
directly. Wrong because at this energy PSD/FCI plateau (their own continuum-vs-energy curve is
nearly flat here, established in 8v's FoM-vs-Energy section) and the "cluster" window is itself
only ~40-50% real captures (8v), so comparing it against a same-plateau continuum gave FoM close to
zero for BOTH metrics -- hiding real discrimination rather than measuring it.

*v2*: restrict scoring to the 2,900-3,400 keVee window alone and split ITS OWN median-seeded value
distribution in half. This produced non-zero, plausible-looking FoM numbers, but checking the
resulting "optimum" against the full spectrum (docs/log/README.md 8v, "Checking the optimum against
the full spectrum") showed it does NOT track the real cluster at all: no bump at 3,160 keVee in the
recomputed vs-Energy plot, and an INVERTED FoM-vs-Energy trend versus the deployed configuration's
own validated one. Splitting a narrow window at its own median has no requirement that the split
track anything physically real -- it can, and did, latch onto some other locally-separable structure
instead of the capture peak. Compounding this, v2 also ran on a corrupted energy axis: see
energy_amplitude's docstring below for a second, independent bug (a wrong per-event baseline
reference) that was smearing the 2,900-3,400 keVee window itself with events from a much wider true
energy range, fixed at the same time as the v2->v3 objective change.

**v3, used below**: the goal stated for this search is the best class separation ACROSS the whole
[2,000, 4,000] keVee region, not just within the narrow window -- so that has to be what gets scored,
while still deriving the split from where the real duality actually lives. At each candidate
configuration: fit the SAME crossing-point cut `derive_separation_cuts` uses elsewhere in this
project (docs/log/README.md 8v) from the 2,900-3,400 keVee window's own median-seeded halves, then
apply that cut to classify EVERY event in the full [2,000, 4,000] keVee population and score
cluster-like vs. continuum-like with this project's usual FoM. This is exactly the same procedure as
one point (LLD=2,000) on the deployed configuration's own validated cumulative FoM-vs-Energy curve
(8v), which is why it is trustworthy where v1 and v2 were not: it is the established, checked
methodology, just swept over candidate configurations instead of held at the deployed one.

FoM is this project's own established convention: |median_cluster_like - median_continuum_like| /
(FWHM_cluster_like + FWHM_continuum_like), robust (IQR-based) sigma -- the same formula
sw/analysis/plot_cosmics_gn_metrics.py uses throughout 8v.

Grid search is adaptive/coarse-to-fine: computing ASDM (for FCI) or the prefix sum (for PSD) once
per trace makes every individual (x, y) candidate a handful of vectorized array lookups, so
resolution is cheap everywhere -- it is increased near the best point purely so the resulting
surface plot is legible (fine detail where it matters, coarse elsewhere), not because a uniformly
fine grid would be too slow.

Run: sw/.venv/bin/python -m analysis.sweep_cosmics_gates   (from sw/)
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 (registers the 3d projection)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.fpga_model import asdm, fci_from_asdm, load_traces, prefix_sums, psd_from_traces

TRACES_PATH = "/home/ivan/datasets/cosmics-CLYC/cosmics_0001_scope_traces_2000_4000keVee.csv"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

# Same peak-based calibration and cluster window as plot_cosmics_gn_metrics.py (8v).
C1_KEVEE_PER_COUNT = 0.48
LLD_KEVEE, ULD_KEVEE = 2000.0, 4000.0
CLUSTER_WINDOW_KEVEE = (2900.0, 3400.0)

# Currently-deployed configuration (this run's own header) -- plotted as a reference point on both
# surfaces so the search result can be read as an improvement (or not) over what was actually run.
DEPLOYED_PSD = dict(short_gate=10, long_gate=32)
DEPLOYED_FCI = dict(l_hi=38, w_hi=120)
PRE_TRIGGER, PRE_GATE = 100, 25
"""These are the GATE ORIGIN this whole sweep is defined against, not incidental bookkeeping: both
PSD gates open at pre_trigger - PRE_GATE (psd_from_traces), so a (short_gate, long_gate) pair found
here is only meaningful when deployed at this same PRE_GATE. That is not a theoretical caveat -- it
already went wrong once. This sweep's short_gate=5 result was found at PRE_GATE=25 (gate opens at
sample 75) and deployed to the device at pre_gate=32 (gate opens at sample 68). Real Cs-137 traces
put the median pulse onset at sample 73, so the same 5-sample gate sat ON the rising edge where it
was optimized and entirely in PRE-PULSE BASELINE where it was deployed -- S/N 4.8 vs 1.9, and a
PSD median of 0.947 vs 0.988. If the device's pre_gate changes, re-run this sweep; do not carry
gate lengths across.

The sweep result itself was sound at its own origin, and the failure above is specifically a
transplant: run live at pre_gate=25 on real hardware, short_gate=5/long_gate=47 held PSD at a
0.91-0.95 median across 475-5800 keVee with only 3.1% of 412,973 events above 1.0
(cosmics_psd_fci_optimized_0001_fci_live.csv). The same pair at pre_gate=32 gives 12.5% above 1.0.
Treat a (short_gate, long_gate) pair and its PRE_GATE as one inseparable triple."""

# PSA_l/PSA_w's shared lower bin edge is swept too (sweep_fci's FCI_LO_RANGE, 0..5), not fixed at
# sw/analysis/tune_fom.py's sweep_fci convention of always fixing it to 1 -- that fixed choice
# turned out to matter a lot on this dataset: bin 1's ASDM magnitude is comparable to bins 2-38's
# entire sum (both ~1-2x10^6, checked directly against these traces), so including it swamps PSA_l's
# narrower band far more than PSA_w's wider one and washed out essentially all energy/shape
# dependence when tried here (every candidate window's FCI collapsed to ~0.90-0.91 regardless of
# energy). bin_accumulator.vhd has one `lo` register shared by both bands, hence one shared sweep
# variable here rather than two independent ones.

FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))
IQR_TO_SIGMA = 1.349


def energy_amplitude(traces: np.ndarray) -> np.ndarray:
    """Plain per-trace peak, matching how `peak` is computed everywhere else this project has
    analyzed cosmics_0001 (the LIST CSV's own `peak` field, ultimately settings.json's calibration
    applied directly to it -- docs/log/README.md 8v).

    An earlier version of this function referenced the peak to a 90-sample pre-trigger baseline
    mean, copying sw/analysis/tune_fom.py's own helper. That was wrong FOR THIS DATASET: these
    traces' pre-trigger region is not flat noise around zero -- its own mean is 360-930 counts
    (median ~595), a real, per-event-variable component, not baseline drift to correct out.
    Subtracting it doesn't remove noise, it ADDS noise relative to the true peak: checked directly,
    the deployed configuration's FoM (see region_fom) came out at 0.22-0.46 depending on scoring
    method with the subtraction, against the real, LIST-CSV-confirmed value of ~1.13-1.15 -- and
    recovered to 1.66 (same order of magnitude, now sane) the moment the subtraction was removed.
    Narrowing the cluster window sharpened the same broken result rather than fixing it (sigma_hi
    fell from 0.024 to 0.004 only once the window shrank to ~150 keVee), which is what pointed at an
    energy-axis noise problem rather than a real physical effect."""
    return traces.max(axis=1)


def robust_fom(a: np.ndarray, b: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 30 or len(b) < 30:
        return float("nan")
    qa, qb = np.percentile(a, [25, 75]), np.percentile(b, [25, 75])
    sa, sb = (qa[1] - qa[0]) / IQR_TO_SIGMA, (qb[1] - qb[0]) / IQR_TO_SIGMA
    denom = FWHM_PER_SIGMA * (sa + sb)
    if denom <= 0:
        return float("nan")
    return float(abs(np.median(a) - np.median(b)) / denom)


def robust_stats(a: np.ndarray):
    q = np.percentile(a, [25, 75])
    return float(np.median(a)), float((q[1] - q[0]) / IQR_TO_SIGMA)


def _gauss(x, mu, sigma):
    return np.exp(-0.5 * ((x - mu) / sigma) ** 2)


def crossing_point(mu_a: float, s_a: float, mu_b: float, s_b: float) -> float:
    x = np.linspace(min(mu_a, mu_b), max(mu_a, mu_b), 4000)
    diff = _gauss(x, mu_a, s_a) - _gauss(x, mu_b, s_b)
    sign_change = np.where(np.diff(np.sign(diff)))[0]
    return float(x[sign_change[0]]) if len(sign_change) else 0.5 * (mu_a + mu_b)


def derive_cut(vals_cluster_window: np.ndarray) -> float:
    """Same procedure as plot_cosmics_gn_metrics.derive_separation_cuts: seed a split at the
    2,900-3,400 keVee window's own median, get each half's robust mu/sigma, and return the crossing
    point of the two resulting Gaussians -- the value at which the two populations are equally
    likely, not just the statistics-only midpoint of medians. Returns NaN if there isn't enough data
    or either half collapses to zero spread."""
    w = vals_cluster_window[np.isfinite(vals_cluster_window)]
    if len(w) < 60:
        return float("nan")
    seed = np.median(w)
    hi, lo = w[w >= seed], w[w < seed]
    if len(hi) < 30 or len(lo) < 30:
        return float("nan")
    mh, sh = robust_stats(hi)
    ml, sl = robust_stats(lo)
    if sh <= 0 or sl <= 0:
        return float("nan")
    return crossing_point(ml, sl, mh, sh)


def region_fom(vals_full_region: np.ndarray, vals_cluster_window: np.ndarray) -> float:
    """This search's actual objective (v3 in the module docstring): derive a cluster/continuum cut
    from the 2,900-3,400 keVee window (derive_cut), then apply that cut to the FULL [2,000, 4,000]
    keVee region and score cluster-like vs. continuum-like there -- so "best" means best separation
    across the region the search was asked to optimize, not just within the narrow training window.
    """
    cut = derive_cut(vals_cluster_window)
    if not np.isfinite(cut):
        return float("nan")
    v = vals_full_region[np.isfinite(vals_full_region)]
    return robust_fom(v[v >= cut], v[v < cut])


def adaptive_grid_search(x_bounds, y_bounds, score_fn, valid_fn,
                          stage_sizes=(15, 11, 11), shrink=0.22):
    """Coarse-to-fine grid search. Stage 1 samples stage_sizes[0] points per axis over the full
    bounds; each following stage re-centers on the best point found so far and shrinks the window
    by `shrink`, sampling stage_sizes[k] points per axis in the smaller window. Returns every
    (x, y, fom, stage) point evaluated across all stages -- `stage` (1, 2, 3, ...) records which
    pass first evaluated that point, so the surface plot can show the adaptive refinement itself
    (see plot_surface) -- and the best (x, y, fom) found.
    """
    x_lo, x_hi = x_bounds
    y_lo, y_hi = y_bounds
    evaluated: dict[tuple[int, int], tuple[float, int]] = {}

    def eval_grid(xs, ys, stage):
        for xf in xs:
            for yf in ys:
                x, y = int(round(xf)), int(round(yf))
                if (x, y) in evaluated or not valid_fn(x, y):
                    continue
                evaluated[(x, y)] = (score_fn(x, y), stage)

    eval_grid(np.linspace(x_lo, x_hi, stage_sizes[0]), np.linspace(y_lo, y_hi, stage_sizes[0]), 1)
    x_span, y_span = float(x_hi - x_lo), float(y_hi - y_lo)
    for stage_idx, n in enumerate(stage_sizes[1:], start=2):
        finite = {k: v[0] for k, v in evaluated.items() if np.isfinite(v[0])}
        if not finite:
            break
        bx, by = max(finite, key=finite.get)
        x_span *= shrink
        y_span *= shrink
        xs = np.linspace(max(x_lo, bx - x_span / 2), min(x_hi, bx + x_span / 2), n)
        ys = np.linspace(max(y_lo, by - y_span / 2), min(y_hi, by + y_span / 2), n)
        eval_grid(xs, ys, stage_idx)

    points = [(x, y, f, s) for (x, y), (f, s) in evaluated.items()]
    finite = {k: v[0] for k, v in evaluated.items() if np.isfinite(v[0])}
    best = max(finite.items(), key=lambda kv: kv[1]) if finite else None
    best = (best[0][0], best[0][1], best[1]) if best is not None else None
    return points, best


def plot_surface(points, best, deployed, xlabel, ylabel, title, out_name):
    """The surface shape alone carries the FoM (height, z); coloring it by height again would
    repeat that same information. Instead the surface is a single neutral color and the scatter
    overlay is colored by SEARCH STAGE -- which is new information the height doesn't show: where
    the coarse pass looked versus where the adaptive refinement concentrated once it found a
    promising region."""
    xs = np.array([p[0] for p in points])
    ys = np.array([p[1] for p in points])
    zs = np.array([p[2] for p in points])
    stages = np.array([p[3] for p in points])
    finite = np.isfinite(zs)
    xs, ys, zs, stages = xs[finite], ys[finite], zs[finite], stages[finite]

    fig = plt.figure(figsize=(10, 8), dpi=140)
    ax = fig.add_subplot(111, projection="3d")
    ax.plot_trisurf(xs, ys, zs, color="lightsteelblue", edgecolor="gray", linewidth=0.15,
                     antialiased=True, alpha=0.5)
    n_stages = int(stages.max())
    sc = ax.scatter(xs, ys, zs, c=stages, cmap="plasma", s=16, vmin=1, vmax=n_stages,
                     edgecolor="black", linewidth=0.2)
    cbar = fig.colorbar(sc, ax=ax, shrink=0.6, label="grid search stage (finer near the optimum)")
    cbar.set_ticks(range(1, n_stages + 1))
    if best is not None:
        ax.scatter([best[0]], [best[1]], [best[2]], color="red", s=90, marker="*",
                   edgecolor="black", label=f"best: ({best[0]}, {best[1]}) -> FoM={best[2]:.3f}")
    dx, dy, dz = deployed
    ax.scatter([dx], [dy], [dz], color="lime", s=70, marker="D", edgecolor="black",
               label=f"deployed: ({dx}, {dy}) -> FoM={dz:.3f}")
    ax.set_xlabel(xlabel); ax.set_ylabel(ylabel); ax.set_zlabel("FoM")
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8)
    plt.tight_layout()
    out = OUT_DIR / out_name
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_best_histogram(values_full_region, cut, fom, xlabel, title, out_name, color):
    """Histograms the FULL [2,000, 4,000] keVee region (what is actually scored), split at the
    cut derived from the 2,900-3,400 keVee window -- not just that narrow window on its own."""
    v = values_full_region[np.isfinite(values_full_region)]
    fig, ax = plt.subplots(figsize=(8, 5.5), dpi=140)
    ax.hist(v[v >= cut], bins=60, color=color, alpha=0.85, label=f"cluster-like (n={int((v >= cut).sum())})")
    ax.hist(v[v < cut], bins=60, color="gray", alpha=0.75, label=f"continuum-like (n={int((v < cut).sum())})")
    ax.axvline(cut, color="black", linestyle="--", linewidth=1.3, label=f"cluster-window cut ({cut:.4f})")
    ax.set_xlabel(xlabel); ax.set_ylabel("counts")
    ax.set_title(f"{title}\nfull-region FoM = {fom:.3f}")
    ax.legend()
    plt.tight_layout()
    out = OUT_DIR / out_name
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def sweep_psd(cum: np.ndarray, cluster_mask: np.ndarray):
    def score(short_gate, long_gate):
        vals = psd_from_traces(cum, PRE_TRIGGER, PRE_GATE, short_gate, long_gate)
        return region_fom(vals, vals[cluster_mask])

    # Same guard as sweep_fci's MIN_GAP, for the same reason: PSD = (long-short)/long shrinks
    # toward 0 as long_gate approaches short_gate, and a tiny PSD scale makes the median-split ratio
    # extremely sensitive to whatever a handful of samples right at the gate boundary happen to
    # contain. Without this guard this sweep picked (short=263, long=275, gap=12): PSD values
    # crammed into [0.023, 0.028] instead of the deployed config's ~[0.75, 0.82] -- a numerically
    # fragile artifact, not a real gate pair, the same failure mode already found and fixed for FCI.
    MIN_GAP = 20

    # MIN_SHORT_FRACTION guards the OPPOSITE end of the same ratio. MIN_GAP only constrains how far
    # apart the two gates are; nothing stops the short gate from carrying so little charge that
    # PSD -> 1 instead of -> 0, and then reading back above 1.0 outright whenever its sum goes
    # negative on noise.
    #
    # Deliberately a charge FRACTION and not a minimum short_gate in samples, because the quantity
    # that actually matters is placement, not length, and a sample count cannot see placement. Both
    # gates open at pre_trigger - PRE_GATE, a few samples ahead of the pulse so the foot of the
    # rising edge is not clipped; how much pulse a short gate contains therefore depends on
    # PRE_GATE as much as on its own length. Measured on real hardware at short_gate=5, the same
    # five samples give:
    #     pre_gate=25 (this sweep's frame): short/long ~ 0.05-0.09, PSD median 0.91-0.95, and only
    #                                        3.1% of a 412,973-event live cosmics run above 1.0
    #     pre_gate=32 (as deployed once):   short/long ~ 0.008-0.011, PSD median 0.99, and 12.5% of
    #                                        a 2,188-event Cs-137 run above 1.0, ranging to 1.21
    # A length-based floor would have rejected the first of those, which is a genuinely good
    # operating point; the fraction separates them cleanly, at either PRE_GATE, with no per-frame
    # retuning.
    MIN_SHORT_FRACTION = 0.02

    def valid(short_gate, long_gate):
        if long_gate < short_gate + MIN_GAP:
            return False
        gs = max(0, PRE_TRIGGER - PRE_GATE)
        n = cum.shape[1] - 1
        se, le = min(n, gs + short_gate), min(n, gs + long_gate)
        short = cum[:, se] - cum[:, gs]
        long_ = cum[:, le] - cum[:, gs]
        ok = long_ > 0
        if not ok.any():
            return False
        return float(np.median(short[ok] / long_[ok])) >= MIN_SHORT_FRACTION

    points, best = adaptive_grid_search((2, 300), (3, 800), score, valid)
    deployed_fom = score(DEPLOYED_PSD["short_gate"], DEPLOYED_PSD["long_gate"])
    print(f"PSD deployed (short={DEPLOYED_PSD['short_gate']}, long={DEPLOYED_PSD['long_gate']}): "
          f"FoM={deployed_fom:.3f}")
    if best is not None:
        print(f"PSD best (short={best[0]}, long={best[1]}): FoM={best[2]:.3f}  "
              f"({len(points)} points evaluated)")
    plot_surface(points, best,
                 (DEPLOYED_PSD["short_gate"], DEPLOYED_PSD["long_gate"], deployed_fom),
                 "short_gate (samples)", "long_gate (samples)",
                 "cosmics_0001: PSD full-region FoM vs (short_gate, long_gate)\n"
                 f"cut from {CLUSTER_WINDOW_KEVEE[0]:.0f}-{CLUSTER_WINDOW_KEVEE[1]:.0f} keVee, scored "
                 f"across {LLD_KEVEE:.0f}-{ULD_KEVEE:.0f} keVee",
                 "cosmics_psd_gate_fom_surface.png")
    if best is not None:
        vals_full = psd_from_traces(cum, PRE_TRIGGER, PRE_GATE, best[0], best[1])
        cut = derive_cut(vals_full[cluster_mask])
        plot_best_histogram(vals_full, cut, best[2], "PSD",
                             f"cosmics_0001: PSD histogram at best grid-search config "
                             f"(short_gate={best[0]}, long_gate={best[1]}), full "
                             f"{LLD_KEVEE:.0f}-{ULD_KEVEE:.0f} keVee region",
                             "cosmics_psd_best_histogram.png", "tab:blue")
    return best, deployed_fom


def sweep_fci(mag: np.ndarray, cluster_mask: np.ndarray):
    """PSA_l and PSA_w share one lower bin edge on real hardware (bin_accumulator.vhd has a single
    `lo` register for both bands, not two independent ones), so `lo` is swept too, not left fixed at
    the deployed value -- but as an OUTER sweep over a small, cheap range (FCI_LO_RANGE, 0..5),
    since it is one shared scalar, not a second independent axis of the 3D plot the way (l_hi, w_hi)
    are. Only the winning `lo`'s own (l_hi, w_hi) surface is plotted, not one surface per lo -- the
    other lo values' results are still searched and reported in the printout, just not plotted.
    """
    nyquist_bin = mag.shape[1] - 1
    FCI_LO_RANGE = range(0, 6)

    # w_hi must clear l_hi by a real margin, not just by 1-2 bins. PSA_w = PSA_l + (a sliver of
    # extra bins) makes the ratio PSA_l/PSA_w extremely sensitive to whatever those few bins happen
    # to contain in THIS finite sample -- a degenerate, numerically fragile "optimum" of exactly the
    # kind tune_fom.py's own module docstring warns a naive grid search will find, not a real,
    # reproducible window. Found by inspection: an earlier version of this sweep without the guard
    # picked (l_hi=194, w_hi=196), a 2-bin gap.
    MIN_GAP = 20

    def valid(l_hi, w_hi):
        return w_hi >= l_hi + MIN_GAP

    results_per_lo = {}
    for lo in FCI_LO_RANGE:
        def score(l_hi, w_hi, lo=lo):
            vals = fci_from_asdm(mag, lo, l_hi, lo, w_hi)
            return region_fom(vals, vals[cluster_mask])

        points, best = adaptive_grid_search((lo + 1, 400), (lo + 2, min(700, nyquist_bin)),
                                             score, valid)
        results_per_lo[lo] = (points, best)
        if best is not None:
            print(f"FCI lo={lo}: best (l_hi={best[0]}, w_hi={best[1]}) -> FoM={best[2]:.3f}  "
                  f"({len(points)} points evaluated)")
        else:
            print(f"FCI lo={lo}: no valid point found")

    lo_best = max((lo for lo in FCI_LO_RANGE if results_per_lo[lo][1] is not None),
                  key=lambda lo: results_per_lo[lo][1][2])
    points, best = results_per_lo[lo_best]
    print(f"FCI overall best: lo={lo_best}, l_hi={best[0]}, w_hi={best[1]} -> FoM={best[2]:.3f}")

    deployed_vals = fci_from_asdm(mag, 2, DEPLOYED_FCI["l_hi"], 2, DEPLOYED_FCI["w_hi"])
    deployed_fom = region_fom(deployed_vals, deployed_vals[cluster_mask])
    print(f"FCI deployed (lo=2, l_hi={DEPLOYED_FCI['l_hi']}, w_hi={DEPLOYED_FCI['w_hi']}): "
          f"FoM={deployed_fom:.3f}")

    plot_surface(points, best, (DEPLOYED_FCI["l_hi"], DEPLOYED_FCI["w_hi"], deployed_fom),
                 "PSA_l upper bin (l_hi)", "PSA_w upper bin (w_hi)",
                 f"cosmics_0001: FCI full-region FoM vs (l_hi, w_hi), best shared lo={lo_best}\n"
                 f"cut from {CLUSTER_WINDOW_KEVEE[0]:.0f}-{CLUSTER_WINDOW_KEVEE[1]:.0f} keVee, scored "
                 f"across {LLD_KEVEE:.0f}-{ULD_KEVEE:.0f} keVee",
                 "cosmics_fci_window_fom_surface.png")
    vals_full = fci_from_asdm(mag, lo_best, best[0], lo_best, best[1])
    cut = derive_cut(vals_full[cluster_mask])
    plot_best_histogram(vals_full, cut, best[2], "FCI",
                         f"cosmics_0001: FCI histogram at best grid-search config "
                         f"(lo={lo_best}, l_hi={best[0]}, w_hi={best[1]}), full "
                         f"{LLD_KEVEE:.0f}-{ULD_KEVEE:.0f} keVee region",
                         "cosmics_fci_best_histogram.png", "tab:red")
    return best, lo_best, deployed_fom


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    traces, lens, _ = load_traces([TRACES_PATH])
    print(f"loaded {len(traces)} raw traces (pre-filtered to peak-based energy in "
          f"[{LLD_KEVEE:.0f}, {ULD_KEVEE:.0f}] keVee on nsil-red)")

    # RAW scope-trace logging duplicates a real fraction of traces (found while debugging this
    # script: 983/3086 = 31.9% of the loaded rows here were sample-for-sample repeats of an earlier
    # row, in groups of up to 7). Each duplicate pair carries a DIFFERENT host_timestamp ~0.2-0.25s
    # apart despite bit-identical samples -- a logging bug (the same captured buffer written out more
    # than once under a fresh timestamp each time), not real repeated events -- so this has to
    # compare trace CONTENT, not the raw CSV lines (which would look unique). Left in, these inflate
    # whichever bins the duplicated events land in and distort every median/IQR computed below.
    # Deduplicating is required, not optional, for this data.
    _, first_idx = np.unique(traces, axis=0, return_index=True)
    first_idx = np.sort(first_idx)
    n_dup = len(traces) - len(first_idx)
    traces = traces[first_idx]
    print(f"deduplicated: removed {n_dup} exact-duplicate rows ({100 * n_dup / (n_dup + len(traces)):.1f}%), "
          f"{len(traces)} unique traces remain")

    keVee = C1_KEVEE_PER_COUNT * energy_amplitude(traces)
    pop = (keVee >= LLD_KEVEE) & (keVee <= ULD_KEVEE)
    traces, keVee = traces[pop], keVee[pop]
    cluster = (keVee >= CLUSTER_WINDOW_KEVEE[0]) & (keVee <= CLUSTER_WINDOW_KEVEE[1])
    print(f"population after local energy check: {len(traces)} traces in "
          f"[{LLD_KEVEE:.0f}, {ULD_KEVEE:.0f}] keVee, {cluster.sum()} of them in the "
          f"{CLUSTER_WINDOW_KEVEE[0]:.0f}-{CLUSTER_WINDOW_KEVEE[1]:.0f} keVee cluster window used "
          f"for scoring")

    cum = prefix_sums(traces)
    sweep_psd(cum, cluster)

    mag = asdm(traces)
    sweep_fci(mag, cluster)  # sweeps its own shared lo internally; see sweep_fci's docstring


if __name__ == "__main__":
    main()
