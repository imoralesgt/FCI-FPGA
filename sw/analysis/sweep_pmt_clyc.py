"""Raw-trace grid search for the PSD gates and FCI windows that best separate neutrons from gammas
on the PMT-coupled CLYC (1"x1", -1400 V), using this project's SiPM search machinery
(sweep_cosmics_gates.py: adaptive_grid_search, plot_surface, robust_fom) unchanged.

What differs from the SiPM search, and why each difference is derived rather than chosen:

* LABELS, not a median split. The SiPM objective (region_fom, "v3") had to infer its classes from
  the 2,900-3,400 keVee window's own median split because nothing else labeled the events. Here
  something does: FCI and PSD, run at their deployed settings, agree on every one of the 1,513
  events in the 6Li window of the live overnight run (docs/log/README.md 10.11). An event both flag
  is a neutron; everything else is a gamma. Neither method alone chooses the labels, so optimizing
  either one against them is not circular.

* The OBJECTIVE is the WORST ENERGY SLICE over [475, 5,800] keVee, not a pooled FoM. For each slice
  (ENERGY_SLICES), the labeled robust FoM of all neutrons against that slice's gammas; the search
  maximizes the minimum over slices. 475 keVee is the NET paper's neutron-detection limit (its
  section 6.1), so low-energy gamma rejection counts. The worst-slice form is not a preference, it
  is what the hardware requirement means: the g/n label must be ONE straight line (10.11), and one
  horizontal line separates every energy only if every energy slice is separated.
  A POOLED FoM over the same range was tried first and is wrong for this: the energy-integrated
  gamma distribution is dominated by hundreds of thousands of low-energy gammas, and both searches
  found settings that pushed that bulk away from the neutrons while letting the HIGH-energy gammas
  -- the ones sitting next to the 6Li neutrons in energy -- move closer. The PSD "optimum" it chose
  scored FoM 0.663 at LLD 2,000 keVee against the deployed setting's 2.827.

* PSD is scored ROBUSTLY against onset jitter. The PMT pulse rises in under one sample and the CFD
  pins its onset at frame sample 56-57, so the gate start (pre_trigger - pre_gate) sits within a
  sample or two of the pulse edge. The first PMT optimum (pre_gate 26 with the old trigger, 10.7)
  was a ridge where a one-sample shift cost two-thirds of the FoM. The search objective is therefore
  the MINIMUM FoM over the gate start shifted by -1, 0, +1 samples: a setting only scores well if
  it survives the one-sample jitter the data actually shows. The non-robust peak is reported too.

* BOUNDS come from the measured pulse (10.12), not from the SiPM ranges. Gate start from 5 samples
  before to 4 after the onset (pre_gate 3..12 at pre_trigger 64). short_gate 1..30: the gamma CVL
  spike is 1-2 samples, the neutron rise ~5 (100 ns). long_gate up to 150 samples (3 us): the mean
  neutron and gamma shapes are identical beyond ~2.5 us, so a longer gate only adds noise. FCI:
  psa_l_hi 10..300 and psa_w_hi 60..1024 bracket the measured neutron/gamma spectral crossover
  (~bin 60) and the bin where the mean spectra converge again (~512); the shared low bin is swept
  over 1..5 as in the SiPM search.

* SATURATED pulses are excluded. 83% of traces the shaper replay places at 4.2-5.8 MeVee have a
  sample at the ADC rail (14,563); below 4.2 MeVee essentially none do. A clipped pulse's FCI and
  PSD describe the clipping, not the scintillation, and on the first run they dominated the top
  energy slice and steered both searches. In hardware the equivalent is to flag saturated events
  rather than classify them.

* FCI results RANK windows; their absolute FoM does not transfer. Offline float FCI differs from
  hardware FCI on identical events (10.6: +0.038 per event, float FoM 18% higher, cause open). PSD
  offline equals hardware exactly. So the PSD optimum's FoM is a prediction of the live value; the
  FCI optimum's FoM is not, and must be confirmed live.

* OPERATING POINTS are profiles (PROFILES below), because every constant above that comes from a
  recording -- deployed gates and windows, shaper replay and calibration, label thresholds, energy
  range and slices -- belongs to one HV/VGA/shaper setting. "pmt1400" is the -1400 V run the
  reasoning above was written for (the default, so its results reproduce unchanged); "dt1050" is the
  DT-mode setting (-1050 V, VGA x4.5, shaper 1/1/1 us), whose slices reach 16.5 MeVee because a DT
  campaign needs one straight line to hold across that whole range.

Run: sw/.venv/bin/python -m analysis.sweep_pmt_clyc <unique_traces.csv> [profile]   (from sw/)
"""

from __future__ import annotations

import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from analysis.fpga_model import asdm, fci_from_asdm, load_traces, prefix_sums, psd_from_traces
from analysis.sweep_cosmics_gates import (
    OUT_DIR, adaptive_grid_search, crossing_point, plot_surface, robust_fom, robust_stats,
)

PRE_TRIGGER = 64                     # locked to the trigger delay by the GUI

PROFILES = {
    # -1400 V, VGA x4.0, shaper 50/1/10 (1.00/0.02/0.20 us). Shaper replay onto the list-mode channel
    # scale (0.9915, the photopeak match of 10.5) and the Cs-137 self-calibration. Labels: FCI and
    # PSD at these deployed settings agree on every 6Li-window event (10.11).
    "pmt1400": dict(
        deployed_psd=dict(pre_gate=6, short_gate=2, long_gate=25),
        deployed_fci=dict(lo=2, l_hi=60, w_hi=512),
        lld=475.0, uld=5800.0,
        shaper=(50, 1, 10), kev_offset=0.0, kev_per_shaper_count=0.9915 / 50.0 * 0.26347,
        label_fci=0.50, label_psd=0.88,
        slices=((475, 700), (700, 1000), (1000, 1500), (1500, 2500), (2500, 3300), (3300, 4200),
                (4200, 5800)),
        out_prefix="pmt_clyc"),
    # DT mode: -1050 V, VGA x4.5, shaper 50/50/50 (1.00/1.00/1.00 us), fixed shaper core (per-frame
    # tap masking), so the replay matches hardware. Calibration c0 -4.9018, c1 2.642 keVee/channel
    # (22Na 511/1275; K-40 at 1,480 and 6Li at 3,328 keVee overnight). LLD 350 keVee is just above the
    # 240-count trigger threshold; ULD 16.5 MeVee is where the live PSD gamma band starts to bend
    # down (the operator's reading of the overnight run), below where the spikiest gammas clip. Labels:
    # FCI 1/50/1/180 and PSD 7/7/34 lines from the first -1050 V neutrons (PSD 0.746 vs gamma max
    # 0.667, FCI 0.670 vs 0.591). Slices extend to 16.5 MeVee: the line must hold where DT gammas and
    # (n,p)/(n,alpha) products land, not just around the 6Li peak.
    "dt1050": dict(
        deployed_psd=dict(pre_gate=7, short_gate=7, long_gate=34),
        deployed_fci=dict(lo=1, l_hi=50, w_hi=180),
        lld=350.0, uld=16500.0,
        shaper=(50, 50, 50), kev_offset=-4.9018, kev_per_shaper_count=2.642 / 50.0,
        label_fci=0.63, label_psd=0.70,
        slices=((350, 700), (700, 1000), (1000, 1500), (1500, 2500), (2500, 5000), (5000, 10000),
                (10000, 16500)),
        out_prefix="pmt_clyc_dt1050"),
}


def configure(profile: str) -> None:
    """Binds the module-level constants the functions below read to one PROFILES entry."""
    global DEPLOYED_PSD, DEPLOYED_FCI, LLD_KEVEE, ULD_KEVEE, SHAPER, KEV_OFFSET
    global KEVEE_PER_SHAPER_COUNT, LABEL_FCI, LABEL_PSD, ENERGY_SLICES, OUT_PREFIX
    p = PROFILES[profile]
    DEPLOYED_PSD, DEPLOYED_FCI = p["deployed_psd"], p["deployed_fci"]
    LLD_KEVEE, ULD_KEVEE = p["lld"], p["uld"]
    SHAPER, KEV_OFFSET, KEVEE_PER_SHAPER_COUNT = p["shaper"], p["kev_offset"], p["kev_per_shaper_count"]
    LABEL_FCI, LABEL_PSD = p["label_fci"], p["label_psd"]
    ENERGY_SLICES, OUT_PREFIX = p["slices"], p["out_prefix"]


configure("pmt1400")

MIN_GAP = 3                          # long_gate >= short_gate + MIN_GAP (same guard as the SiPM PSD sweep)
FCI_MIN_GAP = 20                     # w_hi >= l_hi + FCI_MIN_GAP
RAIL_COUNTS = 14500                  # ADC rail is 14,563 (14-bit full scale less the BLR baseline, 10.5)


def shaper_energy(traces: np.ndarray) -> np.ndarray:
    k, ft, dec = SHAPER
    out = []
    for i in range(0, len(traces), 1000):
        x = traces[i:i + 1000]
        n = x.shape[1]
        pz = np.cumsum(np.hstack([np.zeros((len(x), 1)), x[:, :-1]]), axis=1)
        tr = x + pz / dec

        def sh(m, s):
            o = np.zeros_like(m)
            o[:, s:] = m[:, :n - s]
            return o
        out.append(np.cumsum(tr - sh(tr, k) - sh(tr, k + ft) + sh(tr, 2 * k + ft), axis=1).max(axis=1))
    return KEV_OFFSET + np.concatenate(out) * KEVEE_PER_SHAPER_COUNT


def labeled_fom(vals, neutron):
    return robust_fom(vals[neutron], vals[~neutron])


def worst_slice_fom(vals, neutron, kev):
    """Minimum over ENERGY_SLICES of FoM(all neutrons, that slice's gammas); slices with too few
    gammas for robust_fom (it needs 30) are skipped rather than scored as zero."""
    n = vals[neutron]
    out = []
    for lo, hi in ENERGY_SLICES:
        g = vals[(~neutron) & (kev >= lo) & (kev < hi)]
        f = robust_fom(n, g)
        if np.isfinite(f):
            out.append(f)
    return min(out) if out else float("nan")


def main(path, profile="pmt1400"):
    configure(profile)
    print(f"profile {profile}")
    traces, _, _ = load_traces([path])
    kev = shaper_energy(traces)
    region = (kev >= LLD_KEVEE) & (kev <= ULD_KEVEE)
    clipped = traces.max(axis=1) >= RAIL_COUNTS
    print(f"clipped at the ADC rail and excluded: {int((region & clipped).sum())} of {int(region.sum())} "
          f"traces in range")
    region &= ~clipped
    traces, kev = traces[region], kev[region]
    cum = prefix_sums(traces)
    mag = asdm(traces)

    fci0 = fci_from_asdm(mag, DEPLOYED_FCI["lo"], DEPLOYED_FCI["l_hi"], DEPLOYED_FCI["lo"], DEPLOYED_FCI["w_hi"])
    psd0 = psd_from_traces(cum, PRE_TRIGGER, DEPLOYED_PSD["pre_gate"], DEPLOYED_PSD["short_gate"], DEPLOYED_PSD["long_gate"])
    neutron = (fci0 > LABEL_FCI) & (psd0 > LABEL_PSD)
    print(f"traces in [{LLD_KEVEE:.0f}, {ULD_KEVEE:.0f}] keVee: {len(traces)}  "
          f"neutrons {neutron.sum()}  gammas {(~neutron).sum()}")

    # ---------------------------------------------------------------- PSD
    def psd_fom(pg, s, l):
        return worst_slice_fom(psd_from_traces(cum, PRE_TRIGGER, pg, s, l), neutron, kev)

    def psd_robust(pg):
        return lambda s, l: min(psd_fom(pg + d, s, l) for d in (-1, 0, 1))

    psd_valid = lambda s, l: l >= s + MIN_GAP
    per_pg = {}
    for pg in range(3, 13):
        pts, best = adaptive_grid_search((1, 30), (10, 150), psd_robust(pg), psd_valid)
        per_pg[pg] = (pts, best)
        print(f"  PSD pre_gate {pg:2d} (gate opens at {PRE_TRIGGER - pg}): robust best "
              f"short {best[0]:2d} long {best[1]:3d} -> FoM {best[2]:.3f}  "
              f"(peak at same gates {psd_fom(pg, best[0], best[1]):.3f})")
    pg_best = max(per_pg, key=lambda k: per_pg[k][1][2])
    pts, best = per_pg[pg_best]
    dep = DEPLOYED_PSD
    dep_robust = min(psd_fom(dep["pre_gate"] + d, dep["short_gate"], dep["long_gate"]) for d in (-1, 0, 1))
    plot_surface(pts, best, (dep["short_gate"], dep["long_gate"], dep_robust),
                 "short gate (samples)", "long gate (samples)",
                 f"PMT CLYC: PSD grid search at pre_gate={pg_best} (gate opens at sample "
                 f"{PRE_TRIGGER - pg_best}), pre_trigger={PRE_TRIGGER}\n"
                 f"FoM = worst energy slice ({LLD_KEVEE:.0f}-{ULD_KEVEE:.0f} keVee) and worst gate start +/-1 sample",
                 f"{OUT_PREFIX}_psd_gate_fom_surface.png")

    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=140)
    pgs = sorted(per_pg)
    ax.plot([PRE_TRIGGER - p for p in pgs], [per_pg[p][1][2] for p in pgs], "o-", color="tab:blue",
            label="robust FoM (worst of gate start -1/0/+1)")
    ax.plot([PRE_TRIGGER - p for p in pgs],
            [psd_fom(p, per_pg[p][1][0], per_pg[p][1][1]) for p in pgs], "s--", color="0.5",
            label="peak FoM at the same gates")
    ax.axvspan(56, 57, color="tab:orange", alpha=0.2, label="pulse onset (samples 56-57)")
    ax.set_xlabel("gate start sample (pre_trigger - pre_gate)")
    ax.set_ylabel("best FoM for that gate start")
    ax.set_title("PMT CLYC: best PSD FoM vs where the gates open")
    ax.legend(fontsize=8); ax.grid(alpha=0.3)
    plt.tight_layout(); plt.savefig(OUT_DIR / f"{OUT_PREFIX}_psd_fom_vs_gate_start.png"); plt.close(fig)

    # ---------------------------------------------------------------- FCI
    def fci_fom(lo, lh, wh):
        return worst_slice_fom(fci_from_asdm(mag, lo, lh, lo, wh), neutron, kev)

    fci_valid = lambda lh, wh: wh >= lh + FCI_MIN_GAP
    per_lo = {}
    for lo in range(1, 6):
        pts_f, best_f = adaptive_grid_search((10, 300), (60, 1024), lambda lh, wh, lo=lo: fci_fom(lo, lh, wh), fci_valid)
        per_lo[lo] = (pts_f, best_f)
        print(f"  FCI low bin {lo}: best psa_l_hi {best_f[0]:3d} psa_w_hi {best_f[1]:4d} -> FoM {best_f[2]:.3f}")
    lo_best = max(per_lo, key=lambda k: per_lo[k][1][2])
    pts_f, best_f = per_lo[lo_best]
    d = DEPLOYED_FCI
    plot_surface(pts_f, best_f, (d["l_hi"], d["w_hi"], fci_fom(d["lo"], d["l_hi"], d["w_hi"])),
                 "PSA_l high (bin)", "PSA_w high (bin)",
                 f"PMT CLYC: FCI grid search at low bin={lo_best} (shared by both windows)\n"
                 f"FoM = worst energy slice, {LLD_KEVEE:.0f}-{ULD_KEVEE:.0f} keVee\n"
                 f"offline float: ranks windows, absolute value does not transfer",
                 f"{OUT_PREFIX}_fci_window_fom_surface.png")

    # ---------------------------------------------------------------- summary of the optima
    print("\nOPTIMA vs DEPLOYED, same events:")
    print(f"  worst-slice FoM: PSD deployed {worst_slice_fom(psd0, neutron, kev):.3f} optimum "
          f"{worst_slice_fom(psd_from_traces(cum, PRE_TRIGGER, pg_best, best[0], best[1]), neutron, kev):.3f} | "
          f"FCI deployed {worst_slice_fom(fci0, neutron, kev):.3f} optimum "
          f"{worst_slice_fom(fci_from_asdm(mag, lo_best, best_f[0], lo_best, best_f[1]), neutron, kev):.3f}")
    print("  pooled FoM (the paper's convention) at each LLD:")
    s_opt = psd_from_traces(cum, PRE_TRIGGER, pg_best, best[0], best[1])
    f_opt = fci_from_asdm(mag, lo_best, best_f[0], lo_best, best_f[1])
    for name, v0, v1 in (("PSD", psd0, s_opt), ("FCI", fci0, f_opt)):
        for lld in (LLD_KEVEE, 1000.0, 2000.0):
            m = kev >= lld
            print(f"  {name} LLD {lld:5.0f}: deployed {labeled_fom(v0[m], neutron[m]):.3f}  "
                  f"optimum {labeled_fom(v1[m], neutron[m]):.3f}")
    for name, v in (("PSD optimum", s_opt), ("FCI optimum", f_opt)):
        mn, sn = robust_stats(v[neutron]); mg, sg = robust_stats(v[~neutron])
        cut = crossing_point(mg, sg, mn, sn)
        sign = 1 if mn > mg else -1
        acc = np.mean(sign * (v[neutron] - cut) > 0)
        print(f"  {name}: straight-line cut {cut:.4f}, neutron acceptance {acc:.4f}")
        for lo_e, hi_e in ENERGY_SLICES:
            g = (~neutron) & (kev >= lo_e) & (kev < hi_e)
            k = int((sign * (v[g] - cut) > 0).sum())
            print(f"     gamma leakage {lo_e}-{hi_e} keVee: {k}/{g.sum()}")
    print(f"\nPSD optimum: pre_trigger {PRE_TRIGGER}, pre_gate {pg_best}, short {best[0]}, long {best[1]}")
    print(f"FCI optimum: low bin {lo_best}, psa_l_hi {best_f[0]}, psa_w_hi {best_f[1]}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "pmt1400")
