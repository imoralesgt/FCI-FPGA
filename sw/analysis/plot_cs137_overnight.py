"""Overnight Cs-137 run: ambient neutrons found against a live gamma source.

The first dataset in this project where a 6Li capture peak is resolved while a gamma source sits on
the detector, rather than in a quiet cosmic-ray run. 17.4 h, 15.9 M events at 255 evt/s with Cs-137
close to the crystal, recorded at the validated PSD triple (pre_gate=25, short_gate=5,
long_gate=47) and the optimal FCI windows (lo=2, l_hi=60, w_hi=178) -- see docs/log/README.md's
parameter table.

ENERGY SCALE. Unlike every earlier analysis here, energy does NOT come from
C1_KEVEE_PER_COUNT * peak: `peak` is now pulse_shaper_core's shaped plateau, which scales with
`peaking`, so the old raw-peak constant does not apply. This run carries its own calibration
instead, and a better one than any before it -- Cs-137 puts two known lines directly in the
spectrum:

    Ba K X-ray      32.06 keV  -> channel   66.2
    Cs-137 gamma   661.66 keV  -> channel 1474.4   (12.8% FWHM)

giving E = 2.451 + 0.44711 * channel, channel = peak / peaking. Cross-checked with no free
parameters against the Compton edge: predicted 477 keV = channel 1062, measured steepest fall-off
at channel 1114, the ~5% high bias expected of a resolution-broadened edge.

WHAT IT SHOWS. Selecting FCI > 0.945 -- the empty valley between the two lobes, not a fitted cut --
above 500 keVee returns 537 events whose energy spectrum is an isolated peak at 3074 keVee, not a
continuum: 519 of the 537 fall in 2500-3900 keVee. That is 6Li(n,alpha)t capture (this project's
established ~3160 keVee, 2.7% from the value here, inside this calibration's own uncertainty), at
29.9 neutrons/hour. They cannot come from the source -- Cs-137 is a pure gamma emitter -- so they
are ambient/cosmic.

In the 2900-3400 keVee cluster window the two metrics separate the SAME 855 events very
differently, which is the project's thesis stated on one dataset:

    FCI   neutron median 0.9563, gamma median 0.9282, gap 0.0281, FoM 1.865
    PSD   neutron median 0.9493, gamma median 0.9415, gap 0.0078, FoM 0.964

Run: sw/.venv/bin/python -m analysis.plot_cs137_overnight   (from sw/)
"""
from __future__ import annotations

import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors
import matplotlib.pyplot as plt
import numpy as np

from analysis.plot_cosmics_gn_metrics import robust_fom
from analysis.plot_optimized_psd_fci import fit_double_gaussian, hist_panel

DATA_PATH = "/home/ivan/FciProjects/noDetector/LIST/cs137_0002_fci_live.csv"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "log" / "images"

PEAKING = 50
"""The shaper's `peaking` this run used. `peak` / PEAKING is the channel the calibration below is
defined against -- the same fold the GUI applies (histogram_view.DEFAULT_PEAK_FOLD)."""

CAL_C0, CAL_C1 = 2.451, 0.44711
"""keVee = CAL_C0 + CAL_C1 * channel, from the two Cs-137 lines -- see module docstring."""

CLUSTER_WINDOW = (2900.0, 3400.0)
"""6Li capture window, this project's established bounds (docs/log/README.md 8v)."""

FCI_NEUTRON_CUT = 0.945
"""The empty valley between the gamma and neutron FCI lobes in the cluster window -- a gap with
literally zero counts in it, so it is read off rather than fitted."""


def load() -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Returns (fci, psd, keVee, duration_s) for events with a valid long-gate integral.

    energy_long <= 0 is the documented BLR-gate pathology (project log 8d): firmware reports PSD as
    a 0.0 sentinel for those, so they are not measurements and are dropped rather than plotted.
    """
    ts, fci, e_long, psd, peak = [], [], [], [], []
    with open(DATA_PATH) as f:
        for line in f:
            if line.startswith("#") or line.startswith("timestamp"):
                continue
            r = line.split(",")
            if len(r) < 8:
                continue
            ts.append(int(r[0])); fci.append(float(r[3])); e_long.append(int(r[5]))
            psd.append(float(r[6])); peak.append(int(r[7]))
    ts = np.array(ts, dtype=np.float64)
    fci = np.array(fci); psd = np.array(psd)
    e_long = np.array(e_long); peak = np.array(peak, dtype=np.float64)

    duration_s = (ts[-1] - ts[0]) / 50e6  # 50 MHz hardware timestamp counter
    keep = (e_long > 0) & np.isfinite(fci) & np.isfinite(psd)
    keVee = CAL_C0 + CAL_C1 * (peak / PEAKING)
    print(f"{len(ts):,} events, {duration_s / 3600:.2f} h, {len(ts) / duration_s:.1f} evt/s; "
          f"{(~keep).sum():,} dropped for energy_long <= 0")
    return fci[keep], psd[keep], keVee[keep], duration_s


def plot_vs_energy(fci, psd, keVee):
    """The headline pair: both discriminants against energy, over the full range. The neutron
    cluster is a compact island at 3.1 MeVee, well clear of everything the source produces."""
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5), dpi=140, sharex=True)
    for ax, V, name, ylim in ((axes[0], fci, "FCI", (0.86, 0.99)),
                               (axes[1], psd, "PSD", (0.90, 0.99))):
        m = (keVee >= 100) & (keVee <= 5000)
        h = ax.hist2d(keVee[m], V[m], bins=[320, 260], range=[[100, 5000], ylim],
                      cmap="viridis", norm=matplotlib.colors.LogNorm())
        fig.colorbar(h[3], ax=ax, label="counts/bin")
        ax.axvspan(*CLUSTER_WINDOW, color="white", alpha=0.10)
        ax.set_xlabel("Energy (keVee)")
        ax.set_ylabel(name)
        ax.set_title(f"{name} vs Energy — Cs-137 source, 17.4 h\n"
                     f"shaded: {CLUSTER_WINDOW[0]:.0f}-{CLUSTER_WINDOW[1]:.0f} keVee ⁶Li window")
    plt.tight_layout()
    out = OUT_DIR / "cs137_overnight_fci_psd_vs_energy.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_neutron_spectrum(fci, keVee):
    """Energy spectrum of everything, and of the FCI-selected events alone. The selected set being
    a PEAK rather than a scaled copy of the continuum is what identifies it as 6Li capture."""
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=140)
    bins = np.linspace(100, 5000, 350)
    ax.hist(keVee, bins=bins, histtype="step", color="0.45", label="all events")
    sel = fci > FCI_NEUTRON_CUT
    ax.hist(keVee[sel], bins=bins, histtype="stepfilled", color="tab:red", alpha=0.75,
            label=f"FCI > {FCI_NEUTRON_CUT} (neutron candidates)")
    ax.set_yscale("log")
    ax.set_xlabel("Energy (keVee)"); ax.set_ylabel("counts")
    ax.set_title("Cs-137 overnight run: the FCI-selected population is a ⁶Li capture peak,\n"
                 "not a slice of the gamma continuum")
    ax.legend()
    plt.tight_layout()
    out = OUT_DIR / "cs137_overnight_neutron_spectrum.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def plot_cluster_histograms(fci, psd, keVee):
    """Both discriminants inside the cluster window, with the pooled double-Gaussian fit this
    project uses everywhere else, so the FoM is comparable to every other dataset here."""
    win = (keVee >= CLUSTER_WINDOW[0]) & (keVee <= CLUSTER_WINDOW[1])
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), dpi=140)
    for ax, V, name, cut in ((axes[0], fci, "FCI", FCI_NEUTRON_CUT),
                              (axes[1], psd, "PSD", None)):
        v = V[win]
        cut = cut if cut is not None else float(np.median(v))
        fit = fit_double_gaussian(v[v < cut], v[v >= cut])
        fit["crossing"] = cut
        hist_panel(ax, fit, xlabel=name,
                   title=f"{name} in {CLUSTER_WINDOW[0]:.0f}-{CLUSTER_WINDOW[1]:.0f} keVee\n"
                         f"double-Gaussian fit, FoM = {fit['fom']:.3f}")
    plt.tight_layout()
    out = OUT_DIR / "cs137_overnight_cluster_histograms.png"
    plt.savefig(out); plt.close(fig)
    print(f"saved {out}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fci, psd, keVee, duration_s = load()

    win = (keVee >= CLUSTER_WINDOW[0]) & (keVee <= CLUSTER_WINDOW[1])
    neutron = win & (fci > FCI_NEUTRON_CUT)
    gamma = win & (fci <= FCI_NEUTRON_CUT)
    print(f"\ncluster window: {win.sum():,} events "
          f"({neutron.sum():,} neutron-side, {gamma.sum():,} gamma-side)")
    for name, V in (("FCI", fci), ("PSD", psd)):
        v = V[win]
        cut = FCI_NEUTRON_CUT if name == "FCI" else float(np.median(v))
        print(f"  {name}: FoM {robust_fom(v[v >= cut], v[v < cut]):.3f}   "
              f"neutron median {np.median(V[neutron]):.4f}, gamma median {np.median(V[gamma]):.4f}")

    sel = (fci > FCI_NEUTRON_CUT) & (keVee > 2500) & (keVee < 3900)
    print(f"\n6Li peak: {sel.sum():,} events, centroid {np.mean(keVee[sel]):.0f} keVee, "
          f"FWHM {np.std(keVee[sel]) * 2.355 / np.mean(keVee[sel]) * 100:.1f}%")
    print(f"neutron rate: {sel.sum() / duration_s * 3600:.1f} /hour")

    plot_vs_energy(fci, psd, keVee)
    plot_neutron_spectrum(fci, keVee)
    plot_cluster_histograms(fci, psd, keVee)


if __name__ == "__main__":
    main()
