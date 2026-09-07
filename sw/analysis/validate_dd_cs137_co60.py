"""Offline validation of gamma/neutron (DD) and gamma-only (Cs-137, Co-60) separation on the
2026-09-04 raw-trace dataset (/home/ivan/datasets/clyc-FCI-test-20260904-DD), emulating the FPGA
pipeline exactly as fpga_model.py already does (no extra per-trace baseline correction -- see the
module docstring there: the recorded samples ARE the dev stream the RTL itself works with).

Energy is peak amplitude (traces.max(axis=1), matching dual_gate_integrator.vhd's unconditional
running max -- NOT tune_fom.py's energy_amplitude(), which subtracts an extra pre-trigger mean the
real peak_o register does not; see docs/log/README.md 8p for why that matters for large/saturating
pulses specifically), calibrated with this session's own live c1=0.48 keVee/count (already
cross-checked against the 3160 keVee 6Li capture peak in docs/log/README.md 8n).

Run: sw/.venv/bin/python -m analysis.validate_dd_cs137_co60   (from sw/)
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.fpga_model import asdm, fci_from_asdm, load_traces, prefix_sums, psd_from_traces
from analysis.tune_fom import N_BAND_KEVEE, G_BAND_KEVEE, energy_labels, fom_supervised, guarded_fom
from gui.fom_core import FomFitError, compute_fom

DATA_DIR = "/home/ivan/datasets/clyc-FCI-test-20260904-DD"
C1_KEVEE_PER_COUNT = 0.48
NEUTRON_LIMIT_KEVEE = 475.0  # paper's own lower neutron-detection energy limit (section 6.1)


def dedup_consecutive(traces: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Drops a row that is an exact copy of the row before it FROM THE SAME SOURCE FILE -- a stale
    $RT re-read of a trace the raw-trace pipeline had not yet re-armed for, not a second event."""
    keep = np.ones(len(traces), dtype=bool)
    for i in range(1, len(traces)):
        if src[i] == src[i - 1] and np.array_equal(traces[i], traces[i - 1]):
            keep[i] = False
    n_dropped = (~keep).sum()
    if n_dropped:
        print(f"  dropped {n_dropped} consecutive-duplicate rows (stale $RT reads)")
    return traces[keep]


def load_dataset(pattern: str) -> np.ndarray:
    paths = sorted(glob.glob(pattern))
    print(f"  files: {[Path(p).name for p in paths]}")
    traces, lens, src = load_traces(paths)
    traces = dedup_consecutive(traces, src)
    print(f"  {len(traces)} events after dedup")
    return traces


class Dataset:
    def __init__(self, name: str, pattern: str):
        print(f"Loading {name}...")
        self.name = name
        self.traces = load_dataset(pattern)
        self.peak = self.traces.max(axis=1)
        self.energy = self.peak * C1_KEVEE_PER_COUNT
        self.mag = asdm(self.traces)
        self.cum = prefix_sums(self.traces)

    def fci(self, l_lo, l_hi, w_lo, w_hi):
        return fci_from_asdm(self.mag, l_lo, l_hi, w_lo, w_hi)

    def psd(self, pre_trigger, pre_gate, short_gate, long_gate, baseline_ref=0):
        return psd_from_traces(self.cum, pre_trigger, pre_gate, short_gate, long_gate, baseline_ref)


if __name__ == "__main__":
    dd = Dataset("DD (mixed n/gamma)", f"{DATA_DIR}/dd_*_scope_traces.csv")
    cs137 = Dataset("Cs-137 (gamma only)", f"{DATA_DIR}/cs137_0001_scope_traces.csv")
    co60 = Dataset("Co-60 (gamma only)", f"{DATA_DIR}/co60_0001_scope_traces.csv")

    for ds in (dd, cs137, co60):
        print(f"\n{ds.name}: n={len(ds.traces)}  peak median={np.median(ds.peak):.0f} counts "
              f"({np.median(ds.energy):.0f} keVee)  max={ds.peak.max():.0f} counts")

    PRE_TRIGGER = 100
    WINDOWS = {
        "deployed defaults (acquisition.c / bringup.c)": dict(
            psd=dict(pre_gate=32, short_gate=80, long_gate=250),
            fci=dict(l_lo=1, l_hi=25, w_lo=1, w_hi=90),
        ),
        "session-final (dd_0006/cs137/co60 header)": dict(
            psd=dict(pre_gate=21, short_gate=14, long_gate=40),
            fci=dict(l_lo=0, l_hi=15, w_lo=0, w_hi=150),
        ),
    }

    def try_fit(label, values):
        v = values[np.isfinite(values)]
        try:
            r = compute_fom(v)
            print(f"    {label}: n={len(v)}  mu1={r.mu1:.4g} fwhm1={r.fwhm1:.4g}  "
                  f"mu2={r.mu2:.4g} fwhm2={r.fwhm2:.4g}  separation={r.separation:.4g}  "
                  f"FoM={r.fom:.4f}")
            return r
        except FomFitError as e:
            print(f"    {label}: n={len(v)}  FIT FAILED -- {e}")
            return None

    print("\n" + "=" * 100)
    print("Reference windows: does each dataset show one population (Cs-137, Co-60) or two (DD)?")
    print("=" * 100)
    for wname, w in WINDOWS.items():
        print(f"\n--- {wname} ---")
        print(f"  PSD {w['psd']}   FCI {w['fci']}")
        for ds in (dd, cs137, co60):
            fci_vals = ds.fci(**w["fci"])
            psd_vals = ds.psd(pre_trigger=PRE_TRIGGER, **w["psd"])
            print(f"  {ds.name}:")
            try_fit("FCI (no LLD)          ", fci_vals)
            try_fit("PSD (no LLD)          ", psd_vals)
            lld = ds.energy >= NEUTRON_LIMIT_KEVEE
            print(f"    -- LLD >= {NEUTRON_LIMIT_KEVEE:.0f} keVee keeps {lld.sum()}/{len(ds.traces)} --")
            try_fit(f"FCI (LLD {NEUTRON_LIMIT_KEVEE:.0f} keVee)", fci_vals[lld])
            try_fit(f"PSD (LLD {NEUTRON_LIMIT_KEVEE:.0f} keVee)", psd_vals[lld])
            if ds is dd:
                n_mask, g_mask = energy_labels(ds.energy)
                print(f"    -- supervised bands: neutron {N_BAND_KEVEE} keVee n={n_mask.sum()}, "
                      f"gamma {G_BAND_KEVEE} keVee n={g_mask.sum()} --")
                print(f"    FCI supervised FoM = {fom_supervised(fci_vals, n_mask, g_mask):.4f}")
                print(f"    PSD supervised FoM = {fom_supervised(psd_vals, n_mask, g_mask):.4f}")

    # ---------------------------------------------------------------------- coordinate-wise sweep
    #
    # Same coordinate-wise-one-parameter-at-a-time methodology as the live Optimize tab
    # (fom_sweep_worker.py), scored by fom_supervised (robust to the DD dataset's severe neutron/
    # gamma population imbalance -- see the "no LLD" and "LLD 475" fits above, where the unsupervised
    # double-Gaussian fit degenerated to near-zero separation on PSD) rather than compute_fom.
    # UNLIKE the live tool (docs/log/README.md 8o), psa_l_lo and psa_w_lo are coupled -- swept
    # together, not independently -- since FCI's own definition only means "energy outside the
    # narrow band" when PSA_l is a clean subset of PSA_w.
    n_mask, g_mask = energy_labels(dd.energy)

    def sweep1(label, lo, hi, score_fn, current):
        best_v, best_f = current, score_fn(current)
        for v in range(lo, hi + 1):
            f = score_fn(v)
            if f > best_f:
                best_v, best_f = v, f
        print(f"  {label}: best={best_v} (FoM={best_f:.4f}, started at {current}={score_fn(current):.4f})")
        return best_v

    print("\n" + "=" * 100)
    print("Coordinate-wise sweep on DD, maximizing fom_supervised (coupled PSA lows)")
    print("=" * 100)

    print("\n--- FCI ---")
    lo, l_hi, w_hi = 1, 25, 90  # start from the deployed defaults
    lo = sweep1("lo (l_lo=w_lo, coupled)", 0, 10,
                lambda v: fom_supervised(dd.fci(v, l_hi, v, w_hi), n_mask, g_mask), lo)
    l_hi = sweep1("l_hi", lo + 1, 200,
                  lambda v: fom_supervised(dd.fci(lo, v, lo, w_hi), n_mask, g_mask), l_hi)
    w_hi = sweep1("w_hi", l_hi + 1, 1024,
                  lambda v: fom_supervised(dd.fci(lo, l_hi, lo, v), n_mask, g_mask), w_hi)
    final_fci_fom = fom_supervised(dd.fci(lo, l_hi, lo, w_hi), n_mask, g_mask)
    print(f"  FINAL: psa_l={lo}-{l_hi}  psa_w={lo}-{w_hi}  FoM={final_fci_fom:.4f}")

    # Same long_gate > short_gate physical constraint as the guarded_fom sweep below -- an inverted
    # gate pair gives a nonphysical PSD value that can separate well by coincidence rather than by
    # real pulse shape, so it must be excluded here too, not just where compute_fom's degeneracy
    # made the consequence obvious.
    def sweep_psd_from(pg, sg, lg, label):
        def score(pg_, sg_, lg_):
            if sg_ >= lg_:
                return -1.0
            return fom_supervised(dd.psd(PRE_TRIGGER, pg_, sg_, lg_), n_mask, g_mask)

        print(f"  -- starting from {label}: pre_gate={pg} short_gate={sg} long_gate={lg} "
              f"(FoM={score(pg, sg, lg):.4f}) --")
        pg = sweep1("pre_gate", 0, 100, lambda v: score(v, sg, lg), pg)
        sg = sweep1("short_gate", 1, lg - 1, lambda v: score(pg, v, lg), sg)
        lg = sweep1("long_gate", sg + 1, 1900, lambda v: score(pg, sg, v), lg)
        f = score(pg, sg, lg)
        print(f"  FINAL: pre_gate={pg}  short_gate={sg}  long_gate={lg}  FoM={f:.4f}")
        return pg, sg, lg, f

    print("\n--- PSD (coordinate-wise is seed-dependent -- tried from two starting points) ---")
    r1 = sweep_psd_from(32, 80, 250, "deployed defaults")
    r2 = sweep_psd_from(21, 14, 40, "session-final")
    pg, sg, lg, final_psd_fom = max(r1, r2, key=lambda r: r[3])
    print(f"  BEST OF BOTH: pre_gate={pg}  short_gate={sg}  long_gate={lg}  FoM={final_psd_fom:.4f}")

    # ---------------------------------------------------------------------- combined DD+Cs-137
    #
    # The paper's OWN reported PSD/FCI FoM (1.11 / 1.88, section 6) is computed on "DDTg": their DD
    # AND DT neutron-generator recordings with a SEPARATELY-recorded Cs-137 gamma dataset ADDED in,
    # so the gamma population is deliberately well-populated rather than whatever sparse gamma
    # contamination the generator run happens to contain on its own. This session recorded DD only
    # (no DT), so the equivalent here is DD+Cs-137, not the paper's DD+DT+g. The supervised band-FoM
    # above uses DD alone, where the "gamma" 500-2000 keVee band is a natural minority (n=1232 vs
    # n=9589) of uncertain purity (fast-neutron continuum can land there too) -- not the paper's
    # comparison. Rebuilding DD+Cs-137 the same way, then scoring with compute_fom (the paper's own
    # double-Gaussian method, now well-populated on both sides) is the fairer, paper-matched one.
    print("\n" + "=" * 100)
    print("DD (neutron generator) + Cs-137 (gamma) combined, paper's own DDTg-style method")
    print("=" * 100)
    combo_energy = np.concatenate([dd.energy, cs137.energy])
    combo_lld = combo_energy >= NEUTRON_LIMIT_KEVEE
    print(f"  n = {len(combo_energy)} ({dd.energy.size} DD + {cs137.energy.size} Cs-137), "
          f"{combo_lld.sum()} pass the {NEUTRON_LIMIT_KEVEE:.0f} keVee LLD")

    def combo_fci(l_lo, l_hi, w_lo, w_hi):
        return np.concatenate([dd.fci(l_lo, l_hi, w_lo, w_hi), cs137.fci(l_lo, l_hi, w_lo, w_hi)])

    def combo_psd(pre_gate, short_gate, long_gate):
        return np.concatenate([dd.psd(PRE_TRIGGER, pre_gate, short_gate, long_gate),
                                cs137.psd(PRE_TRIGGER, pre_gate, short_gate, long_gate)])

    for wname, w in WINDOWS.items():
        print(f"\n--- {wname} ---")
        try_fit("FCI (LLD 475, DD+Cs137)", combo_fci(**w["fci"])[combo_lld])
        try_fit("PSD (LLD 475, DD+Cs137)", combo_psd(**w["psd"])[combo_lld])
    print(f"\n--- offline-swept window (FCI {lo}-{l_hi}/{lo}-{w_hi}, PSD pg={pg} sg={sg} lg={lg}) ---")
    try_fit("FCI (LLD 475, DD+Cs137)", combo_fci(lo, l_hi, lo, w_hi)[combo_lld])
    try_fit("PSD (LLD 475, DD+Cs137)", combo_psd(pg, sg, lg)[combo_lld])

    print("\n--- re-sweeping on DD+Cs137+LLD with guarded_fom (paper's method, degeneracy-guarded) ---")
    print("    (an UNGUARDED compute_fom().fom sweep here first found pre_gate=100/short=103/")
    print("     long=151 with FoM=82 -- gate_start=max(0,pre_trigger-100)=0 collapses one fitted")
    print("     peak to near-zero width, a numerical artifact, not a real optimum. This is the")
    print("     exact failure mode tune_fom.py's guarded_fom exists to reject -- see docs/log 8p.)")

    def fom_of(values):
        return guarded_fom(values[np.isfinite(values)])

    print("FCI:")
    lo2, l_hi2, w_hi2 = 1, 25, 90
    lo2 = sweep1("lo (coupled)", 0, 10,
                 lambda v: fom_of(combo_fci(v, l_hi2, v, w_hi2)[combo_lld]), lo2)
    l_hi2 = sweep1("l_hi", lo2 + 1, 200,
                   lambda v: fom_of(combo_fci(lo2, v, lo2, w_hi2)[combo_lld]), l_hi2)
    w_hi2 = sweep1("w_hi", l_hi2 + 1, 1024,
                   lambda v: fom_of(combo_fci(lo2, l_hi2, lo2, v)[combo_lld]), w_hi2)
    print(f"  FINAL: psa_l={lo2}-{l_hi2}  psa_w={lo2}-{w_hi2}  "
          f"FoM={fom_of(combo_fci(lo2, l_hi2, lo2, w_hi2)[combo_lld]):.4f}")

    # long_gate must exceed short_gate -- tune_fom.py's OWN sweep_psd already guards this
    # ("if lg <= sg: continue"), a guard this script's ad-hoc coordinate sweep had dropped. Without
    # it the short_gate step is free to push past whatever long_gate happens to be at that moment
    # (e.g. short_gate=344 against long_gate=250, an inverted, unphysical gate pair) and land on
    # a numerically-inflated FoM from that degenerate region -- exactly the kind of implausible
    # PSD FoM (>3, some runs >10) flagged as unphysical: real reported PSD FoMs top out around
    # 1.1-1.5 in the literature this project cites (Nakhostin, Dutta, the paper itself).
    def psd_score(pg_, sg_, lg_):
        if sg_ >= lg_:
            return -1.0
        return fom_of(combo_psd(pg_, sg_, lg_)[combo_lld])

    print("PSD:")
    for seed_pg, seed_sg, seed_lg, label in [(32, 80, 250, "deployed"), (21, 14, 40, "session-final")]:
        print(f"  from {label}:")
        pg2, sg2, lg2 = seed_pg, seed_sg, seed_lg
        pg2 = sweep1("  pre_gate", 0, 100, lambda v: psd_score(v, sg2, lg2), pg2)
        sg2 = sweep1("  short_gate", 1, lg2 - 1, lambda v: psd_score(pg2, v, lg2), sg2)
        lg2 = sweep1("  long_gate", sg2 + 1, 1900, lambda v: psd_score(pg2, sg2, v), lg2)
        print(f"    FINAL: pre_gate={pg2} short_gate={sg2} long_gate={lg2}  "
              f"FoM={psd_score(pg2, sg2, lg2):.4f}")

    # ---------------------------------------------------------------------- Cs-137 anomaly re-check
    #
    # docs/log/README.md 8n found an unexplained second FCI/PSD-vs-Energy band in the LIVE Cs-137
    # run. Hypothesis: it is the double-Gaussian fit locking onto an energy-dependent VARIANCE
    # funnel (low-energy events noisier/more spread in FCI or PSD, high-energy events tighter) --
    # not two genuine particle populations -- which would appear identically in Co-60 (confirmed
    # above: both show a "second population" with no LLD, at comparable FoM to DD's own no-LLD fit,
    # which is itself contaminated by the same effect). Checked directly here: within narrow energy
    # slices, is the FCI distribution itself unimodal (funnel) or does it stay two-humped even at
    # fixed energy (a real second population)?
    print("\n" + "=" * 100)
    print("Is the Cs-137/Co-60 'second band' a real population, or energy-dependent variance?")
    print("=" * 100)
    for ds in (cs137, co60):
        fci_vals = ds.fci(lo, l_hi, lo, w_hi)
        print(f"\n{ds.name} -- FCI at the DD-optimized window ({lo}-{l_hi}/{lo}-{w_hi}):")
        edges = np.percentile(ds.energy, np.linspace(0, 100, 7))
        for i in range(len(edges) - 1):
            m = (ds.energy >= edges[i]) & (ds.energy < edges[i + 1])
            v = fci_vals[m]
            v = v[np.isfinite(v)]
            if len(v) < 20:
                continue
            iqr = np.percentile(v, 75) - np.percentile(v, 25)
            print(f"  {edges[i]:6.0f}-{edges[i+1]:6.0f} keVee (n={len(v):4d}): "
                  f"median={np.median(v):.4f}  IQR={iqr:.4f}  std={v.std():.4f}")
