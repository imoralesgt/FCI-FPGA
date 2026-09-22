"""Recomputes PSD and FCI for every event in the cosmics_0001 raw scope-trace log, "as if it was
done in the FPGA" with the grid-search-optimal configuration found by
sw/analysis/sweep_cosmics_gates.py (docs/log/README.md 8v):

    PSD: short_gate=5, long_gate=47      (deployed was short_gate=10, long_gate=32)
    FCI: lo=2, l_hi=60, w_hi=178         (deployed was lo=2, l_hi=38, w_hi=120)

Meant to run WHERE THE RAW TRACE FILE LIVES (nsil-red, not this sandbox -- see the
remote-dev-machine project memory): `cosmics_0001_scope_traces.csv` is 1.95 GB, so this reads and
processes it in batches rather than loading it whole, and is deliberately self-contained (the
PSD/FCI equations are re-derived inline, matching sw/analysis/fpga_model.py's dual_gate_integrator
/bin_accumulator model exactly) rather than importing that module, so it has no dependency on this
package's layout being present on the machine it runs on.

Also deduplicates by exact trace content (hashed, not stored verbatim, to keep memory bounded): the
raw scope-trace log was found to contain a real fraction of duplicated traces (31.9% in the [2,000,
4,000] keVee slice sweep_cosmics_gates.py searches on) -- the SAME captured samples logged twice,
~0.2-0.25s apart, under two DIFFERENT host_timestamps each time (checked directly: duplicate groups'
timestamps differ while every sample is bit-identical). That means the hash MUST exclude
host_timestamp -- hashing the whole line, as an earlier version of this script did, missed nearly
all of them (61/273,724 instead of the true rate) because the timestamp made every "duplicate" line
look unique. Left in, these double-count those events in every downstream plot -- which is what
first produced a spurious extra population when this bug was still present (see docs/log/README.md
8v for the retraction of that result).

Output is a small per-event CSV (host_timestamp, energy_kevee, psd_opt, fci_opt) -- energy is the
PLAIN per-trace peak (`traces.max(axis=1)`), matching the `peak` field used everywhere else this
project has analyzed cosmics_0001 (settings.json's calibration applied directly to it, docs/log/
README.md 8v). An earlier version of this script referenced the peak to a 90-sample pre-trigger
baseline mean (sw/analysis/tune_fom.py's energy_amplitude convention) -- wrong for this dataset:
that pre-trigger region's own mean is 360-930 counts (median ~595), not baseline noise to subtract
out, and doing so smeared the energy axis enough to corrupt the grid search built on top of it (see
sw/analysis/sweep_cosmics_gates.py's energy_amplitude docstring for the numbers). Meant to be copied
back and consumed by sw/analysis/plot_cosmics_optimal_psd_fci.py.

Run (on the machine holding the raw trace file): python compute_cosmics_optimal_psd_fci.py
"""
import hashlib
import time

import numpy as np

IN_PATH = r"C:\Users\Ivan\FciProjects\test-windows\RAW\cosmics_0001_scope_traces.csv"
OUT_PATH = r"C:\Users\Ivan\FciProjects\test-windows\RAW\cosmics_0001_optimal_psd_fci.csv"

FFT_LENGTH = 2048
C1_KEVEE_PER_COUNT = 0.48

PRE_TRIGGER, PRE_GATE = 100, 25
PSD_SHORT, PSD_LONG = 5, 47             # grid-search optimum, docs/log/README.md 8v
FCI_LO, FCI_L_HI, FCI_W_HI = 2, 60, 178  # grid-search optimum, docs/log/README.md 8v

BATCH = 4000


def flush(ts_list, traces, fout):
    traces = np.vstack(traces)
    keVee = C1_KEVEE_PER_COUNT * traces.max(axis=1)

    # PSD: dual_gate_integrator.vhd's (long - short) / long, half-open gate ends.
    z = np.zeros((traces.shape[0], 1))
    cum = np.concatenate([z, np.cumsum(traces, axis=1)], axis=1)
    n = cum.shape[1] - 1
    gs = max(0, PRE_TRIGGER - PRE_GATE)
    se, le = min(n, gs + PSD_SHORT), min(n, gs + PSD_LONG)
    short = cum[:, se] - cum[:, gs]
    long_ = cum[:, le] - cum[:, gs]
    with np.errstate(divide="ignore", invalid="ignore"):
        psd = np.where(long_ > 0, (long_ - short) / long_, np.nan)

    # FCI: bin_accumulator.vhd's PSA_l / PSA_w, inclusive bin ranges over the ASDM (|Re|+|Im|).
    spec = np.fft.rfft(traces, n=FFT_LENGTH, axis=1)
    mag = np.abs(spec.real) + np.abs(spec.imag)
    c = np.cumsum(mag, axis=1)

    def band(lo, hi):
        hi = min(hi, mag.shape[1] - 1)
        return c[:, hi] - (c[:, lo - 1] if lo > 0 else 0.0)

    psa_l = band(FCI_LO, FCI_L_HI)
    psa_w = band(FCI_LO, FCI_W_HI)
    with np.errstate(divide="ignore", invalid="ignore"):
        fci = np.where(psa_w > 0, psa_l / psa_w, np.nan)

    for i in range(len(ts_list)):
        fout.write(f"{ts_list[i]},{keVee[i]:.4f},{psd[i]:.6f},{fci[i]:.6f}\n")
    return len(ts_list)


def main():
    t0 = time.time()
    n_read = 0
    n_dup = 0
    n_written = 0
    seen = set()
    ts_buf, trace_buf = [], []

    with open(IN_PATH, "r") as fin, open(OUT_PATH, "w") as fout:
        fout.write("host_timestamp,energy_kevee,psd_opt,fci_opt\n")
        for line in fin:
            if line.startswith("#") or line.startswith("host_timestamp"):
                continue
            n_read += 1
            parts = line.rstrip("\n").split(",")
            # Hash the SAMPLES ONLY (parts[1:] = n_samples + every sample), excluding
            # host_timestamp (parts[0]) -- see module docstring for why the timestamp must be
            # excluded to catch this dataset's actual duplicate-logging pattern.
            h = hashlib.md5(",".join(parts[1:]).encode()).digest()
            if h in seen:
                n_dup += 1
                continue
            seen.add(h)

            n = int(parts[1])
            v = np.asarray(parts[2:2 + n], dtype=np.float64)
            if n < FFT_LENGTH:
                v = np.concatenate([v, np.zeros(FFT_LENGTH - n)])
            ts_buf.append(parts[0])
            trace_buf.append(v)

            if len(ts_buf) >= BATCH:
                n_written += flush(ts_buf, trace_buf, fout)
                ts_buf, trace_buf = [], []

            if n_read % 20000 == 0:
                print(f"{n_read} read, {n_dup} dup, {n_written} written, {time.time()-t0:.1f}s",
                      flush=True)

        if ts_buf:
            n_written += flush(ts_buf, trace_buf, fout)

    print(f"DONE: {n_read} read, {n_dup} dup, {n_written} written, {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
