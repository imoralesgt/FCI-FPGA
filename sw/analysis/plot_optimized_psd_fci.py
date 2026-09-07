"""PSD/FCI vs Energy plus double-Gaussian FoM histograms for the 2026-09-04 CLYC dataset, in the
style of the paper's Fig. 5 (scatter, class-separation line) and Fig. 7 (histogram + double-Gaussian
fit). See docs/log/README.md 8s.

Both discriminators are computed on the RAW captured traces -- no pile-up rejection, no CFD
re-alignment. 8r added both and they are NOT used here: the hardware CFD's own alignment is what the
instrument actually has, and the PSD gates below were found by grid search directly on the raw
traces, anchored to the window that was hand-tuned live against the DD beam
(pre_gate=21/short=14/long=40, "Screenshot From 2026-09-04 15-03-19").

Class-separation lines are placed at the CROSSING POINT of the two fitted Gaussians, not at the
midpoint of the class medians. The midpoint is a statistics-only choice and put the FCI line through
the 6Li cluster; the crossing point is where the two populations are equally likely, which is the
physically meaningful boundary and the one the histogram panel makes visible.

Classes for fitting and FoM use the energy regions with low cross-contamination:
  neutron : the 6Li capture core (2950-3350 keVee) plus the fast-neutron continuum above 3500 keVee
  gamma   : Cs-137 + Co-60 events between the paper's 475 keVee neutron limit and 2000 keVee

Run: sw/.venv/bin/python -m analysis.plot_optimized_psd_fci   (from sw/)
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.fpga_model import asdm, fci_from_asdm, load_traces, prefix_sums, psd_from_traces

DATA_DIR = "/home/ivan/datasets/clyc-FCI-test-20260904-DD"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

C1_KEVEE_PER_COUNT = 0.49327
C0_KEVEE = -40.35
"""TWO-POINT energy calibration, E = c1 * peak_adc + c0, anchored on both features this dataset
actually contains, Gaussian-fitted centroids in both cases:

    Cs-137 photopeak   1423.9 ADC counts  ->  662 keV   (FWHM 11.8%)
    6Li(n,alpha)t line 6488.1 ADC counts  -> 3160 keVee (FWHM  9.5%)

A single-point scaling through the origin (the earlier 0.4895, anchored on the 6Li line alone) put
the Cs-137 photopeak at 697 keVee, 5.3% high -- visible directly as the 662 keV marker sitting off
the Cs-137 centroid in the gamma-only plot. The response simply does not extrapolate through zero.

The paper fits three points including the baseline (its Table 1 / Fig. 2, E = 9.024x - 2.54, also a
negative offset). Doing the same here gives c1 = 0.48869, c0 = -14.83, which splits the difference
and leaves Cs-137 at 681 keVee. The two-point form is used instead because it puts BOTH known
features exactly where they belong, which is the point of the exercise."""

LI6_LINE_KEVEE = 3160.0
LI6_CORE_KEVEE = (2950.0, 3350.0)
FAST_N_MIN_KEVEE = 3500.0     # above the capture line: fast-neutron continuum, no gamma contribution
GAMMA_RANGE_KEVEE = (475.0, 2000.0)
NEUTRON_LIMIT_KEVEE = 475.0
"""The paper's own lower neutron-detection limit (section 6.1): 517 keVee for the lowest-energy fast
neutrons, 476 after folding in the detector's ~8% resolution."""
PRE_TRIGGER = 100

FCI_LINE_GAMMA_PERCENTILE = 99.0
"""FCI's discrimination line is placed at this percentile of the GAMMA population above the neutron
detection limit -- the gamma band's upper envelope at the lowest energy where a neutron can exist at
all -- rather than at the fitted double-Gaussian crossing. It lands at 0.894 against the crossing's
0.908, and is the better-founded choice: the crossing is a statement about two fitted curves, while
this is a statement about where real gamma events actually stop, at the energy that matters.

It also performs better where it counts: 98.0% of the 6Li cluster kept versus 97.4%, for 99.2% gamma
rejection versus 99.96%. The rule is NOT applied to PSD -- there the two populations overlap so
heavily (8s) that the 99th gamma percentile lands above the neutron centroid and would reject most
of the neutrons. That it works for one method and not the other is itself the measurement."""

FCI_WINDOW = dict(l_lo=2, l_hi=38, w_lo=2, w_hi=120)
"""Validated in 8p and left exactly as found -- only the separation line needed fixing."""

PSD_WINDOW = dict(pre_gate=25, short_gate=10, long_gate=32)
"""Grid-searched on raw traces starting from the window hand-tuned live against the DD beam
(21/14/40, "Screenshot From 2026-09-04 15-03-19"), with the paper's 475 keVee LLD applied before
scoring and THREE constraints, each of which an earlier unconstrained search violated (8s):

  1. PHYSICS: median PSD of neutrons must EXCEED that of gammas. With PSD = (long-short)/long, a
     neutron's longer decay puts more charge in the late part of the window, so it must sit higher.
     Optimizers that ignore the sign happily return inverted windows -- one such (32/4/25) scored
     FoM 0.82 with neutrons BELOW gammas, which is not a PSD at all.
  2. Not jammed against the PSD=1 border: median_n kept inside [0.68, 0.82], the regime the live
     hand-tuned window sits in (0.737). A short gate small enough to push the ratio to 0.95+ is
     integrating almost nothing and stops being the paper's quantity.
  3. `long_gate` >= 28 so the long integral is never near zero (8p's failure mode 3).

Result: FoM 0.632 against the hand-tuned window's 0.455 -- a modest, believable improvement rather
than a large one, which is what the physics allows. Medians 0.7958 (n) / 0.7775 (gamma)."""

LLD_CUTS_KEVEE = [0, 100, 200, 300, 475, 700, 1000, 1500, 2000]
IQR_TO_SIGMA = 1.349
FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))


def dedup_consecutive(traces, src):
    keep = np.ones(len(traces), dtype=bool)
    for i in range(1, len(traces)):
        if src[i] == src[i - 1] and np.array_equal(traces[i], traces[i - 1]):
            keep[i] = False
    return traces[keep]


def load_dataset(pattern):
    traces, lens, src = load_traces(sorted(glob.glob(pattern)))
    return dedup_consecutive(traces, src)


def calibrate(traces):
    """Peak amplitude in ADC counts -> keVee, via the two-point calibration above."""
    return traces.max(axis=1) * C1_KEVEE_PER_COUNT + C0_KEVEE


def _gauss(x, a, mu, sigma):
    return a * np.exp(-((x - mu) ** 2) / (2 * sigma ** 2))


def _double_gauss(x, a1, m1, s1, a2, m2, s2):
    return _gauss(x, a1, m1, s1) + _gauss(x, a2, m2, s2)


def fit_double_gaussian(gamma_vals, neutron_vals, bins=120):
    """Fit ONE six-parameter double-Gaussian to the POOLED distribution, as the paper does for its
    own Fig. 7 -- not two independently fitted curves. The two classes are still histogrammed
    separately for display (their energy labels are known), but the fitted model, the FWHMs and the
    FoM all come from the single pooled fit, which is what makes the number comparable to the
    paper's. The separation line is the CROSSING POINT of the two fitted components: the value at
    which an event is equally likely to belong to either."""
    g = gamma_vals[np.isfinite(gamma_vals)]
    n = neutron_vals[np.isfinite(neutron_vals)]
    # Bin over a range wide enough to cover the eventual display window (gamma centroid - 5 sigma to
    # neutron centroid + 5 sigma). Binning on data percentiles instead truncated the fitted curve
    # before the display limit -- the FCI panel visibly stopped at 0.94.
    qg, qn = np.percentile(g, [25, 75]), np.percentile(n, [25, 75])
    sg_seed, sn_seed = (qg[1] - qg[0]) / IQR_TO_SIGMA, (qn[1] - qn[0]) / IQR_TO_SIGMA
    lo = min(np.median(g) - 6 * sg_seed, np.median(n) - 6 * sn_seed,
             np.percentile(g, 0.5), np.percentile(n, 0.5))
    hi = max(np.median(g) + 6 * sg_seed, np.median(n) + 6 * sn_seed,
             np.percentile(g, 99.5), np.percentile(n, 99.5))
    edges = np.linspace(lo, hi, bins + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    cg, _ = np.histogram(g, bins=edges)
    cn, _ = np.histogram(n, bins=edges)
    pooled, _ = np.histogram(np.concatenate([g, n]), bins=edges)

    # Seed from each class's own robust location/width -- a std-based seed is thrown off by the
    # tails and the pooled fit then fails to converge on the tighter windows.
    def robust_seed(vals, counts):
        q = np.percentile(vals, [25, 75])
        return [float(counts.max()), float(np.median(vals)),
                max((q[1] - q[0]) / IQR_TO_SIGMA, 1e-5)]

    p0 = robust_seed(g, cg) + robust_seed(n, cn)
    lo_b = [0, lo, 1e-6, 0, lo, 1e-6]
    hi_b = [np.inf, hi, hi - lo, np.inf, hi, hi - lo]
    try:
        popt, _ = curve_fit(_double_gauss, centers, pooled, p0=p0, bounds=(lo_b, hi_b),
                            maxfev=200000)
    except RuntimeError:
        # Pooled fit did not converge: fall back to the seed itself, which is already a robust
        # per-class estimate. Reported so a fallback is never mistaken for a converged fit.
        print("  (pooled double-Gaussian did not converge; using robust per-class estimates)")
        popt = np.array(p0)
    c1, c2 = popt[:3], popt[3:]
    # Assign each fitted component to the class whose OWN median it sits closer to. Ordering by
    # mean instead (lowest = gamma) is wrong: PSD's polarity flips with gate geometry, and for
    # some windows the neutron population is the lower-valued one.
    med_g, med_n = float(np.median(g)), float(np.median(n))
    if abs(c1[1] - med_g) + abs(c2[1] - med_n) <= abs(c1[1] - med_n) + abs(c2[1] - med_g):
        (ag, mg, sg_), (an, mn, sn) = c1, c2
    else:
        (an, mn, sn), (ag, mg, sg_) = c1, c2
    sg_, sn = abs(sg_), abs(sn)

    # crossing point between the two fitted Gaussians, searched between the two centroids
    x = np.linspace(min(mg, mn), max(mg, mn), 4000)
    diff = _gauss(x, ag, mg, sg_) - _gauss(x, an, mn, sn)
    sign_change = np.where(np.diff(np.sign(diff)))[0]
    crossing = float(x[sign_change[0]]) if len(sign_change) else 0.5 * (mg + mn)

    fwhm_g, fwhm_n = FWHM_PER_SIGMA * sg_, FWHM_PER_SIGMA * sn
    fom = abs(mn - mg) / (fwhm_g + fwhm_n) if (fwhm_g + fwhm_n) > 0 else float("nan")
    return dict(centers=centers, counts_gamma=cg, counts_neutron=cn, counts_pooled=pooled,
                params_gamma=(ag, mg, sg_), params_neutron=(an, mn, sn), params_all=popt,
                crossing=crossing, fom=fom, fwhm_gamma=fwhm_g, fwhm_neutron=fwhm_n)


def robust_fom(neutron, gamma):
    a, b = neutron[np.isfinite(neutron)], gamma[np.isfinite(gamma)]
    if len(a) < 50 or len(b) < 50:
        return float("nan")
    qa, qb = np.percentile(a, [25, 75]), np.percentile(b, [25, 75])
    sa, sb = (qa[1] - qa[0]) / IQR_TO_SIGMA, (qb[1] - qb[0]) / IQR_TO_SIGMA
    denom = FWHM_PER_SIGMA * (sa + sb)
    return abs(np.median(a) - np.median(b)) / denom if denom > 0 else float("nan")


def point_density(x, y, bins=200, x_range=None, y_range=None):
    """Per-point density from a 2D histogram binned over the DISPLAYED range, not the data range.

    Binning over the data range makes the resolution depend on the outliers: PSD spans -0.24 to 1.90
    (near-zero long integrals at low energy), so 200 bins over that left only 37 inside the visible
    [0.6, 1.0] window, against 167 for FCI's much tighter range -- the 6Li cluster came out visibly
    blockier in one panel than the other for no reason but the axis limits.
    """
    rng = [x_range if x_range else [x.min(), x.max()],
           y_range if y_range else [y.min(), y.max()]]
    h, xe, ye = np.histogram2d(x, y, bins=bins, range=rng)
    xi = np.clip(np.digitize(x, xe) - 1, 0, bins - 1)
    yi = np.clip(np.digitize(y, ye) - 1, 0, bins - 1)
    return h[xi, yi]


Y_HALF_SPAN = 0.075
"""Scatter panels are zoomed to +/- this around the class-separation line. Both discriminators
occupy a narrow band on this detector, and even [0.6, 1.0] left the class structure squeezed."""


def shade_cumulative_llds(ax, cuts, xmax):
    """Vertical line at each LLD cut, region beyond it shaded progressively darker -- the sibling
    project's cumulative-cut convention, showing which events each successive cut retains."""
    cuts = [c for c in cuts if 0 < c < xmax]
    for i, c in enumerate(cuts):
        ax.axvline(c, color="black", linewidth=0.6, alpha=0.5, zorder=1)
        right = cuts[i + 1] if i + 1 < len(cuts) else xmax
        ax.axvspan(c, right, color="gray", alpha=0.10 + 0.40 * (i + 1) / len(cuts), zorder=0)


def scatter_panel(ax, energy, values, ylabel, title, line, xmax, log_x=False, shade_llds=False):
    ok = np.isfinite(values)
    x, y = energy[ok], values[ok]
    ylim = (line - Y_HALF_SPAN, line + Y_HALF_SPAN)
    if log_x:
        lo_x = max(1.0, float(np.min(x[x > 0])) * 0.9)
        xr = [np.log10(lo_x), np.log10(xmax)]
        d = point_density(np.log10(np.clip(x, 1, None)), y, x_range=xr, y_range=list(ylim))
    else:
        d = point_density(x, y, x_range=[0, xmax], y_range=list(ylim))
    o = np.argsort(d)
    ax.scatter(x[o], y[o], c=d[o], cmap="jet", s=3, edgecolors="none", rasterized=True)
    if shade_llds:
        shade_cumulative_llds(ax, LLD_CUTS_KEVEE, xmax)
    ax.axhline(line, color="black", linestyle="--", linewidth=1.4, zorder=4,
                label=f"class separation ({line:.3f})")
    ax.axvline(LI6_LINE_KEVEE, color="magenta", linestyle="-.", linewidth=1.1, zorder=4,
                label=f"$^6$Li capture, {LI6_LINE_KEVEE:.0f} keVee")
    if log_x:
        ax.set_xscale("log")
        ax.set_xlim(max(80.0, lo_x), xmax)
    else:
        ax.set_xlim(0, xmax)
    ax.set_ylim(*ylim)
    ax.set_xlabel("Energy (keVee)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(loc="lower right", fontsize=7)


def hist_panel(ax, fit, xlabel, title):
    """Pooled data and the double-Gaussian fit only -- the individual components are deliberately
    not drawn. X range is 5 sigma beyond each class centroid, on the side that class sits."""
    c = fit["centers"]
    ax.step(c, fit["counts_pooled"], where="mid", color="0.35", lw=1.0, label="experimental data")
    ax.step(c, fit["counts_gamma"], where="mid", color="tab:blue", alpha=0.45, label="$\\gamma$ subset")
    ax.step(c, fit["counts_neutron"], where="mid", color="tab:red", alpha=0.45, label="n subset")
    fine = np.linspace(c[0], c[-1], 800)
    ax.plot(fine, _double_gauss(fine, *fit["params_all"]), color="black", lw=1.8,
            label="double-Gaussian fit")
    ax.axvline(fit["crossing"], color="black", linestyle="--", lw=1.4,
                label=f"separation ({fit['crossing']:.3f})")
    _, mg, sg_ = fit["params_gamma"]
    _, mn, sn = fit["params_neutron"]
    # [gamma centroid - 5 sigma_gamma, neutron centroid + 5 sigma_neutron]. Neutrons sit above
    # gammas by physics (8s), so this is simply lower class outward, upper class outward.
    ax.set_xlim(mg - 5 * sg_, mn + 5 * sn)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Counts")
    ax.set_title(title)
    ax.legend(fontsize=7)


if __name__ == "__main__":
    print("Loading...")
    dd = load_dataset(f"{DATA_DIR}/dd_*_scope_traces.csv")
    cs = load_dataset(f"{DATA_DIR}/cs137_0001_scope_traces.csv")
    co = load_dataset(f"{DATA_DIR}/co60_0001_scope_traces.csv")
    gamma = np.vstack([cs, co])
    dd_E = calibrate(dd)
    gamma_E = np.concatenate([calibrate(cs), calibrate(co)])
    print(f"  DD {len(dd)}, Cs-137 {len(cs)}, Co-60 {len(co)}  (raw traces, no rejection, no alignment)")

    dd_fci = fci_from_asdm(asdm(dd), **FCI_WINDOW)
    gamma_fci = fci_from_asdm(asdm(gamma), **FCI_WINDOW)
    dd_psd = psd_from_traces(prefix_sums(dd), PRE_TRIGGER, **PSD_WINDOW)
    gamma_psd = psd_from_traces(prefix_sums(gamma), PRE_TRIGGER, **PSD_WINDOW)

    # Physically clean classes: 6Li core + fast-neutron continuum vs pure-gamma runs above the LLD.
    n_mask = ((dd_E >= LI6_CORE_KEVEE[0]) & (dd_E < LI6_CORE_KEVEE[1])) | (dd_E >= FAST_N_MIN_KEVEE)
    g_mask = (gamma_E >= GAMMA_RANGE_KEVEE[0]) & (gamma_E < GAMMA_RANGE_KEVEE[1])
    print(f"  classes: neutron {n_mask.sum()} (6Li core + >{FAST_N_MIN_KEVEE:.0f} keVee), "
          f"gamma {g_mask.sum()} ({GAMMA_RANGE_KEVEE[0]:.0f}-{GAMMA_RANGE_KEVEE[1]:.0f} keVee)")

    fit_psd = fit_double_gaussian(gamma_psd[g_mask], dd_psd[n_mask])
    fit_fci = fit_double_gaussian(gamma_fci[g_mask], dd_fci[n_mask])
    print(f"\nPSD {PSD_WINDOW}: fitted FoM={fit_psd['fom']:.3f}  separation at {fit_psd['crossing']:.4f}")
    print(f"    gamma mu={fit_psd['params_gamma'][1]:.4f} sigma={fit_psd['params_gamma'][2]:.4f}  "
          f"neutron mu={fit_psd['params_neutron'][1]:.4f} sigma={fit_psd['params_neutron'][2]:.4f}")
    print(f"FCI {FCI_WINDOW}: fitted FoM={fit_fci['fom']:.3f}  "
          f"fitted crossing at {fit_fci['crossing']:.4f}")

    # FCI's line: gamma envelope at the neutron detection limit, not the fitted crossing (see
    # FCI_LINE_GAMMA_PERCENTILE). PSD keeps the crossing -- the rule does not transfer, its classes
    # overlap too much.
    gamma_above_limit = gamma_fci[gamma_E >= NEUTRON_LIMIT_KEVEE]
    gamma_above_limit = gamma_above_limit[np.isfinite(gamma_above_limit)]
    fci_line = float(np.percentile(gamma_above_limit, FCI_LINE_GAMMA_PERCENTILE))
    psd_line = fit_psd["crossing"]
    n_fci = dd_fci[n_mask]; n_fci = n_fci[np.isfinite(n_fci)]
    g_fci = gamma_fci[g_mask]; g_fci = g_fci[np.isfinite(g_fci)]
    print(f"    FCI line from the gamma p{FCI_LINE_GAMMA_PERCENTILE:.0f} above "
          f"{NEUTRON_LIMIT_KEVEE:.0f} keVee = {fci_line:.4f}  "
          f"(keeps {100 * (n_fci >= fci_line).mean():.1f}% of neutrons, "
          f"rejects {100 * (g_fci < fci_line).mean():.1f}% of gammas)")
    print(f"    gamma mu={fit_fci['params_gamma'][1]:.4f} sigma={fit_fci['params_gamma'][2]:.4f}  "
          f"neutron mu={fit_fci['params_neutron'][1]:.4f} sigma={fit_fci['params_neutron'][2]:.4f}")

    all_E = np.concatenate([dd_E, gamma_E])
    all_psd = np.concatenate([dd_psd, gamma_psd])
    all_fci = np.concatenate([dd_fci, gamma_fci])
    xmax = float(all_E.max()) * 1.02

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    scatter_panel(axes[0][0], all_E, all_psd, "PSD = (long-short)/long",
                   f"CLYC: PSD vs Energy (pre_gate={PSD_WINDOW['pre_gate']}, "
                   f"short={PSD_WINDOW['short_gate']}, long={PSD_WINDOW['long_gate']})",
                   psd_line, xmax)
    hist_panel(axes[0][1], fit_psd, "PSD", f"PSD double-Gaussian fit, FoM = {fit_psd['fom']:.2f}")
    scatter_panel(axes[1][0], all_E, all_fci, "FCI = PSA_l/PSA_w",
                   f"CLYC: FCI vs Energy (psa_l={FCI_WINDOW['l_lo']}-{FCI_WINDOW['l_hi']}, "
                   f"psa_w={FCI_WINDOW['w_lo']}-{FCI_WINDOW['w_hi']})",
                   fci_line, xmax)
    hist_panel(axes[1][1], fit_fci, "FCI", f"FCI double-Gaussian fit, FoM = {fit_fci['fom']:.2f}")
    fig.tight_layout()
    p1 = OUT_DIR / "dd_psd_fci_vs_energy_optimized_shaded.png"
    fig.savefig(p1, dpi=140)
    print(f"Wrote {p1}")

    # ---- log-energy view with the cumulative LLD shading, the sibling project's
    # 13_psd_fci_matrices_cumulative_shaded.png layout. Kept as its own figure because the log axis
    # is what makes the low-energy decades legible, and the low-energy behaviour is the hypothesis
    # under test -- the linear panels above compress everything below ~1000 keVee into one edge.
    fig3, axes3 = plt.subplots(2, 1, figsize=(9, 10))
    scatter_panel(axes3[0], all_E, all_psd, "PSD = (long-short)/long",
                   f"CLYC: PSD vs Energy, cumulative LLD shading (pre_gate="
                   f"{PSD_WINDOW['pre_gate']}, short={PSD_WINDOW['short_gate']}, "
                   f"long={PSD_WINDOW['long_gate']})",
                   psd_line, xmax, log_x=True, shade_llds=True)
    scatter_panel(axes3[1], all_E, all_fci, "FCI = PSA_l/PSA_w",
                   f"CLYC: FCI vs Energy, cumulative LLD shading (psa_l="
                   f"{FCI_WINDOW['l_lo']}-{FCI_WINDOW['l_hi']}, psa_w={FCI_WINDOW['w_lo']}-"
                   f"{FCI_WINDOW['w_hi']})",
                   fci_line, xmax, log_x=True, shade_llds=True)
    fig3.tight_layout()
    p3 = OUT_DIR / "dd_psd_fci_vs_energy_log_shaded.png"
    fig3.savefig(p3, dpi=140)
    print(f"Wrote {p3}")

    # ---- gamma-only sources against the SAME lines: the paper's Fig. 6 to the plots above's Fig. 5.
    # Neither source can produce a neutron, so every event above the line is a misclassification and
    # the false-neutron rate is measured directly rather than inferred from a fit.
    print("\nGamma-only sources against the DD-derived lines "
          f"(PSD {psd_line:.4f}, FCI {fci_line:.4f}):")
    cs_E_ = calibrate(cs)
    co_E_ = calibrate(co)
    sources = [
        ("Cs-137", cs, cs_E_, 662.0, "$^{137}$Cs photopeak, 662 keV"),
        ("Co-60", co, co_E_, 1253.0, "$^{60}$Co mean, 1253 keV"),
    ]
    fig4, axes4 = plt.subplots(2, 2, figsize=(15, 10))
    for col, (name, traces, energy, mark_kev, mark_label) in enumerate(sources):
        vals_psd = psd_from_traces(prefix_sums(traces), PRE_TRIGGER, **PSD_WINDOW)
        vals_fci = fci_from_asdm(asdm(traces), **FCI_WINDOW)
        for row, (vals, line, label, meth) in enumerate(
                [(vals_psd, psd_line, "PSD = (long-short)/long", "PSD"),
                 (vals_fci, fci_line, "FCI = PSA_l/PSA_w", "FCI")]):
            v = vals[np.isfinite(vals)]
            e = energy[np.isfinite(vals)]
            false_n = float((v >= line).mean())
            above_limit = e >= NEUTRON_LIMIT_KEVEE
            false_n_cut = float((v[above_limit] >= line).mean()) if above_limit.sum() else float("nan")
            print(f"  {name:7s} {meth}: {100 * false_n:5.2f}% above the line "
                  f"(full range, n={len(v)});  {100 * false_n_cut:5.2f}% above "
                  f"{NEUTRON_LIMIT_KEVEE:.0f} keVee (n={int(above_limit.sum())})")
            ax = axes4[row][col]
            scatter_panel(ax, e, v, label,
                           f"{name} (gamma only): {meth} vs Energy -- "
                           f"{100 * false_n:.2f}% above the line",
                           line, xmax)
            ax.axvline(mark_kev, color="darkgreen", linestyle=":", linewidth=1.2, zorder=4,
                        label=mark_label)
            ax.legend(loc="lower right", fontsize=7)
    fig4.tight_layout()
    p4 = OUT_DIR / "gamma_only_psd_fci_vs_energy.png"
    fig4.savefig(p4, dpi=140)
    print(f"Wrote {p4}")

    # ---------------------------------------------------------------- FoM vs LLD
    print("\nFoM vs LLD cut (no ULD):")
    rows = {"PSD": [], "FCI": []}
    for lld in LLD_CUTS_KEVEE:
        # The gamma class here is bounded ABOVE only (2000 keVee, to stay pure); its lower edge is
        # the LLD itself. Using GAMMA_RANGE_KEVEE's 475 floor would make every cut below 475 a
        # no-op and give bit-identical FoM across five different LLDs.
        nm = n_mask & (dd_E >= lld)
        gm = (gamma_E >= lld) & (gamma_E < GAMMA_RANGE_KEVEE[1])
        if gm.sum() < 50:
            print(f"  LLD>={lld}: gamma reference exhausted ({gm.sum()}) -- stopping")
            break
        f_psd = robust_fom(dd_psd[nm], gamma_psd[gm])
        f_fci = robust_fom(dd_fci[nm], gamma_fci[gm])
        rows["PSD"].append((lld, int(nm.sum()), int(gm.sum()), f_psd))
        rows["FCI"].append((lld, int(nm.sum()), int(gm.sum()), f_fci))
        print(f"  LLD>={lld:5d}  n_n={nm.sum():5d} n_g={gm.sum():5d}   PSD={f_psd:.3f}   FCI={f_fci:.3f}")

    fig2, ax2 = plt.subplots(figsize=(9, 5.5))
    xs_all = list(range(len(LLD_CUTS_KEVEE)))
    for label, marker, color in (("PSD", "o", "tab:blue"), ("FCI", "s", "tab:red")):
        xs = [LLD_CUTS_KEVEE.index(r[0]) for r in rows[label]]
        ys = [r[3] for r in rows[label]]
        ax2.plot(xs, ys, marker=marker, color=color, label=label)
    ax2.set_xticks(xs_all)
    ax2.set_xticklabels([str(c) for c in LLD_CUTS_KEVEE], rotation=45)
    ax2.set_xlabel("Lower energy cut, ENERGY > X (keVee)")
    ax2.set_ylabel("FoM = S / (FWHM$_n$ + FWHM$_\\gamma$)")
    ax2.set_title("CLYC: FoM vs cumulative LLD cut")
    ax2.legend()
    ax2.grid(alpha=0.3)
    fig2.tight_layout()
    p2 = OUT_DIR / "fom_vs_lld_psd_fci_summary.png"
    fig2.savefig(p2, dpi=140)
    print(f"Wrote {p2}")

    print("\nMarkdown table:\n| LLD (keVee) | n neutron | n gamma | PSD FoM | FCI FoM |\n|---|---|---|---|---|")
    for (lld, nn, ng, fp), (_, _, _, ff) in zip(rows["PSD"], rows["FCI"]):
        print(f"| {lld} | {nn} | {ng} | {fp:.3f} | {ff:.3f} |")
