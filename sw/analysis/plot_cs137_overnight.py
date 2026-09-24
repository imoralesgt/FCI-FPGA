"""Overnight Cs-137 runs: ambient neutrons resolved against a live gamma source.

The first datasets in this project where a 6Li capture peak is resolved while a gamma source sits on
the detector, rather than in a quiet cosmic-ray run. Two independent overnight runs, both at the
validated PSD triple (pre_gate=25, short_gate=5, long_gate=47) and the optimal FCI windows (lo=2,
l_hi=60, w_hi=178) -- see docs/log/README.md's parameter table.

SELF-CALIBRATING, per run, and that is not a convenience. Energy cannot come from the old
C1_KEVEE_PER_COUNT * peak: `peak` is pulse_shaper_core's shaped plateau, which scales with
`peaking`, so the raw-peak constant does not apply. It also cannot be a single constant shared
across runs -- the Cs-137 photopeak moved from channel 1474.4 to 1495.7 between these two (1.4%
gain drift over one day), so each run is calibrated against its OWN two Cs-137 lines:

    Ba K X-ray      32.06 keV
    Cs-137 gamma   661.66 keV

Fitting both per run is what makes the two runs' energy axes comparable at all.

WHAT THEY SHOW. Cutting at FCI > FCI_NEUTRON_CUT -- an empty valley BETWEEN the two lobes, read off
rather than fitted -- returns a population whose energy spectrum is an isolated peak at ~3.05-3.07
MeVee, not a slice of the gamma continuum. That is 6Li(n,alpha)t capture at ~27-30 neutrons/hour,
measured straight through a gamma flux outnumbering it by roughly 30,000:1. Cs-137 is a pure gamma
emitter, so the neutrons are ambient/cosmic.

The two runs reproduce each other closely (see the comparison main() prints), and FCI out-separates
PSD by the same margin in both -- the project's thesis, stated twice on independent data under
deliberate gamma load.

Run: sw/.venv/bin/python -m analysis.plot_cs137_overnight   (from sw/)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit

from analysis.plot_optimized_psd_fci import fit_double_gaussian, hist_panel

LIST_DIR = Path("/home/ivan/FciProjects/noDetector/LIST")
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

RUNS = [("cs137_0002_fci_live.csv", "run 2 (2026-09-22, 17.4 h)"),
        ("cs137_0003_fci_live.csv", "run 3 (2026-09-23, 22.4 h)")]

PEAKING = 50
"""The shaper's `peaking` both runs used. `peak / PEAKING` is the channel, the same fold the GUI
applies (histogram_view.DEFAULT_PEAK_FOLD)."""

BA_KEV, CS_KEV = 32.06, 661.66
CLUSTER_WINDOW = (2900.0, 3400.0)
"""6Li capture window, this project's established bounds (docs/log/README.md 8v)."""

FCI_NEUTRON_CUT = 0.945
"""The empty valley between the gamma and neutron FCI lobes. Both runs put 1 event or fewer in the
bins either side of it, so it is read off rather than fitted -- and the SAME value works for both,
which is itself a result."""


@dataclass
class Run:
    label: str
    fci: np.ndarray
    psd: np.ndarray
    keVee: np.ndarray
    duration_s: float
    cal: tuple[float, float]
    photopeak_ch: float
    photopeak_fwhm: float
    psd_over_one: float


def _load_columns(path: Path):
    """Returns (ts, fci, energy_long, psd, peak), tolerating both CSV schemas.

    The column set gained `energy_cal` (and `peak` was renamed `energy`) partway through these
    runs; `peak` stayed at index 7 precisely so positional readers like this one keep working, so
    the only thing that varies is the row width.
    """
    ts, fci, e_long, psd, peak = [], [], [], [], []
    with open(path) as f:
        for line in f:
            if line.startswith("#") or line.startswith("timestamp"):
                continue
            r = line.split(",")
            if len(r) < 8:
                continue
            ts.append(int(r[0])); fci.append(float(r[3])); e_long.append(int(r[5]))
            psd.append(float(r[6])); peak.append(int(r[7]))
    return (np.array(ts, dtype=np.float64), np.array(fci), np.array(e_long),
            np.array(psd), np.array(peak, dtype=np.float64))


def _fit_line(ch: np.ndarray, lo: float, hi: float, mu0: float):
    """Gaussian-on-a-linear-background fit, returning (centroid, sigma)."""
    def model(x, A, mu, sig, m, b):
        return A * np.exp(-(x - mu) ** 2 / (2 * sig ** 2)) + m * x + b
    h, edges = np.histogram(ch, bins=120, range=(lo, hi))
    c = (edges[:-1] + edges[1:]) / 2
    p, _ = curve_fit(model, c, h, p0=[h.max(), mu0, (hi - lo) / 12, 0, h.min()], maxfev=30000)
    return p[1], abs(p[2])


def load_run(filename: str, label: str) -> Run:
    ts, fci, e_long, psd, peak = _load_columns(LIST_DIR / filename)
    duration_s = (ts[-1] - ts[0]) / 50e6  # 50 MHz hardware timestamp counter
    # energy_long <= 0 is the documented BLR-gate pathology (project log 8d): firmware reports PSD
    # as a 0.0 sentinel there, so those are not measurements.
    keep = (e_long > 0) & np.isfinite(fci) & np.isfinite(psd)
    fci, psd, ch = fci[keep], psd[keep], peak[keep] / PEAKING

    mu_cs, sig_cs = _fit_line(ch, 1280, 1750, 1480)
    mu_ba, _ = _fit_line(ch, 40, 110, 70)
    c1 = (CS_KEV - BA_KEV) / (mu_cs - mu_ba)
    c0 = BA_KEV - c1 * mu_ba

    print(f"\n=== {label} ===")
    print(f"  {len(ts):,} events, {duration_s / 3600:.2f} h, {len(ts) / duration_s:.1f} evt/s"
          f"  ({(~keep).sum():,} dropped for energy_long <= 0)")
    print(f"  Cs-137 photopeak channel {mu_cs:.1f}, FWHM {sig_cs * 2.355 / mu_cs * 100:.2f}%")
    print(f"  calibration  E = {c0:+.3f} + {c1:.5f} * channel")
    print(f"  PSD > 1.0: {(psd > 1).mean() * 100:.2f}%")
    return Run(label, fci, psd, c0 + c1 * ch, duration_s, (c0, c1),
               mu_cs, sig_cs * 2.355 / mu_cs * 100, (psd > 1).mean() * 100)


def summarize(run: Run) -> dict:
    win = (run.keVee >= CLUSTER_WINDOW[0]) & (run.keVee <= CLUSTER_WINDOW[1])
    n, g = win & (run.fci > FCI_NEUTRON_CUT), win & (run.fci <= FCI_NEUTRON_CUT)
    iqr = lambda x: (np.percentile(x, 75) - np.percentile(x, 25)) / 1.349
    out = {"window": int(win.sum()), "n": int(n.sum()), "g": int(g.sum())}
    for name, V in (("fci", run.fci), ("psd", run.psd)):
        gap = abs(np.median(V[n]) - np.median(V[g]))
        out[f"{name}_n"] = float(np.median(V[n]))
        out[f"{name}_g"] = float(np.median(V[g]))
        out[f"{name}_gap"] = float(gap)
        out[f"{name}_fom"] = float(gap / (2.355 * (iqr(V[n]) + iqr(V[g]))))
    sel = (run.fci > FCI_NEUTRON_CUT) & (run.keVee > 2500) & (run.keVee < 3900)
    out["peak_n"] = int(sel.sum())
    out["centroid"] = float(np.mean(run.keVee[sel]))
    out["fwhm"] = float(np.std(run.keVee[sel]) * 2.355 / np.mean(run.keVee[sel]) * 100)
    out["rate_per_h"] = sel.sum() / run.duration_s * 3600
    # Poisson only -- the honest error bar on a counting measurement, before any systematic.
    out["rate_err"] = np.sqrt(sel.sum()) / run.duration_s * 3600
    return out


def plot_vs_energy(run: Run, tag: str):
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5), dpi=140, sharex=True)
    for ax, V, name, ylim in ((axes[0], run.fci, "FCI", (0.86, 0.99)),
                               (axes[1], run.psd, "PSD", (0.90, 0.99))):
        m = (run.keVee >= 100) & (run.keVee <= 5000)
        h = ax.hist2d(run.keVee[m], V[m], bins=[320, 260], range=[[100, 5000], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm())
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvspan(*CLUSTER_WINDOW, color="white", alpha=0.10)
        ax.set_xlabel("Energy (keVee)"); ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy — {run.label}\n"
                     f"shaded: {CLUSTER_WINDOW[0]:.0f}-{CLUSTER_WINDOW[1]:.0f} keVee ⁶Li window")
    plt.tight_layout()
    out = OUT_DIR / f"cs137_{tag}_fci_psd_vs_energy.png"
    plt.savefig(out); plt.close(fig); print(f"  saved {out.name}")


def plot_neutron_spectrum(run: Run, tag: str):
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=140)
    bins = np.linspace(100, 5000, 350)
    ax.hist(run.keVee, bins=bins, histtype="step", color="0.45", label="all events")
    ax.hist(run.keVee[run.fci > FCI_NEUTRON_CUT], bins=bins, histtype="stepfilled",
            color="tab:red", alpha=0.75, label=f"FCI > {FCI_NEUTRON_CUT} (neutron candidates)")
    ax.set_yscale("log")
    ax.set_xlabel("Energy (keVee)"); ax.set_ylabel("counts")
    ax.set_title(f"{run.label}: the FCI-selected population is a ⁶Li capture peak,\n"
                 "not a slice of the gamma continuum")
    ax.legend()
    plt.tight_layout()
    out = OUT_DIR / f"cs137_{tag}_neutron_spectrum.png"
    plt.savefig(out); plt.close(fig); print(f"  saved {out.name}")


def plot_cluster_histograms(run: Run, tag: str):
    win = (run.keVee >= CLUSTER_WINDOW[0]) & (run.keVee <= CLUSTER_WINDOW[1])
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), dpi=140)
    for ax, V, name, cut in ((axes[0], run.fci, "FCI", FCI_NEUTRON_CUT),
                              (axes[1], run.psd, "PSD", None)):
        v = V[win]
        cut = cut if cut is not None else float(np.median(v))
        fit = fit_double_gaussian(v[v < cut], v[v >= cut])
        fit["crossing"] = cut
        hist_panel(ax, fit, xlabel=name,
                   title=f"{name} in {CLUSTER_WINDOW[0]:.0f}-{CLUSTER_WINDOW[1]:.0f} keVee — "
                         f"{run.label}\ndouble-Gaussian fit, FoM = {fit['fom']:.3f}")
    plt.tight_layout()
    out = OUT_DIR / f"cs137_{tag}_cluster_histograms.png"
    plt.savefig(out); plt.close(fig); print(f"  saved {out.name}")


def plot_run_comparison(runs: list[Run], stats: list[dict]):
    """The two runs' FCI lobes overlaid, area-normalized. Independent nights, different trigger
    thresholds, independently fitted energy calibrations -- if the neutron lobe sits in the same
    place in both, that is the measurement reproducing, not one dataset's quirk."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.0), dpi=140)
    bins = np.linspace(0.90, 0.98, 90)
    for run, st in zip(runs, stats):
        win = (run.keVee >= CLUSTER_WINDOW[0]) & (run.keVee <= CLUSTER_WINDOW[1])
        axes[0].hist(run.fci[win], bins=bins, histtype="step", density=True,
                     label=f"{run.label} — FoM {st['fci_fom']:.3f}")
        axes[1].hist(run.psd[win], bins=np.linspace(0.925, 0.965, 90), histtype="step",
                     density=True, label=f"{run.label} — FoM {st['psd_fom']:.3f}")
    axes[0].axvline(FCI_NEUTRON_CUT, color="k", ls="--", lw=1)
    axes[0].set_xlabel("FCI"); axes[1].set_xlabel("PSD")
    for ax, name in ((axes[0], "FCI"), (axes[1], "PSD")):
        ax.set_ylabel("normalized counts"); ax.legend(fontsize=8)
        ax.set_title(f"{name} in the ⁶Li window, both runs overlaid")
    plt.tight_layout()
    out = OUT_DIR / "cs137_overnight_run_comparison.png"
    plt.savefig(out); plt.close(fig); print(f"saved {out.name}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    runs, stats, tags = [], [], ["overnight", "overnight3"]
    for (filename, label), tag in zip(RUNS, tags):
        run = load_run(filename, label)
        st = summarize(run)
        print(f"  ⁶Li peak: {st['peak_n']:,} events, centroid {st['centroid']:.0f} keVee, "
              f"FWHM {st['fwhm']:.1f}%, rate {st['rate_per_h']:.1f} ± {st['rate_err']:.1f} /h")
        print(f"  cluster window {st['window']:,} ({st['n']} n / {st['g']} γ): "
              f"FCI FoM {st['fci_fom']:.3f}, PSD FoM {st['psd_fom']:.3f}")
        plot_vs_energy(run, tag); plot_neutron_spectrum(run, tag)
        plot_cluster_histograms(run, tag)
        runs.append(run); stats.append(st)

    print("\n=== the two runs side by side ===")
    rows = [("photopeak channel", "{photopeak_ch:.1f}"), ("photopeak FWHM %", "{photopeak_fwhm:.2f}"),
            ("PSD > 1.0 %", "{psd_over_one:.2f}")]
    for lbl, fmt in rows:
        print(f"  {lbl:<22} " + "  ".join(f"{fmt.format(**vars(r)):>12}" for r in runs))
    for lbl, key, fmt in (("⁶Li events", "peak_n", "{:,}"), ("centroid keVee", "centroid", "{:.0f}"),
                           ("peak FWHM %", "fwhm", "{:.1f}"), ("rate /h", "rate_per_h", "{:.1f}"),
                           ("FCI neutron median", "fci_n", "{:.4f}"),
                           ("FCI gamma median", "fci_g", "{:.4f}"),
                           ("FCI gap", "fci_gap", "{:.4f}"), ("FCI FoM", "fci_fom", "{:.3f}"),
                           ("PSD gap", "psd_gap", "{:.4f}"), ("PSD FoM", "psd_fom", "{:.3f}")):
        print(f"  {lbl:<22} " + "  ".join(f"{fmt.format(s[key]):>12}" for s in stats))

    plot_run_comparison(runs, stats)


if __name__ == "__main__":
    main()
