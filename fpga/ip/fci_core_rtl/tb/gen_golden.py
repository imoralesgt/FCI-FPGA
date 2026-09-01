#!/usr/bin/env python3
"""Builds the integration testbench's stimulus + golden reference from REAL detector traces.

Source: traces recorded on hardware 2026-08-28 under a DD neutron generator (CLYC:Ce + SiPM),
captured at trigger depth 2048 -- the same length this core's FFT now transforms, so they need no
resampling or decimation to be valid stimulus. That is why these are used in preference to
data/fci_verification_set.csv, which is 2048 samples at the paper's 100 Msps (a 20.48 us window)
and would represent a different bin-to-frequency mapping than this core's 2048 @ 50 Msps (40.96 us).

The golden values are NOT independently derived physics -- they are this project's own definition
of the algorithm (|Re| + |Im| summed over inclusive bin windows, exactly what bin_accumulator.vhd
computes) evaluated in float. What they verify is that the assembled RTL -- framer packing, the
FFT IP's bit-reversed output ordering, and the accumulator's un-reversal -- agrees with the
intended spectral math on real pulse shapes. A wrong bit-reversal or a swapped Re/Im half would
show up immediately; both are exactly the kind of wiring mistake that is invisible in a
single-tone test but ruins discrimination on real data.

Compare the RATIO, not the absolute sums
----------------------------------------
The FFT is configured for block floating point, so every frame is scaled by its own exponent --
chosen from that frame's own magnitude, and deliberately discarded by fci_core_rtl_top (it cancels
in the FCI ratio, which is the only thing firmware computes from these). Absolute psa_l/psa_w
therefore differ from a float reference by an arbitrary per-frame power of two: measured here,
2^8 for strong pulses and 2^4 for weak ones. Only psa_l/psa_w is scale-invariant and meaningful,
so that is what golden.txt carries and what the testbench checks.

Usage:  python3 gen_golden.py   (writes stimulus.txt + golden.txt beside this script)
"""
import csv
import pathlib

import numpy as np

FFT_LENGTH = 2048
N_TRACES = 8

# Windows chosen to span the interesting region for this detector: psa_l narrow around the pulse's
# spectral corner (~bin 2-3 at 50 Msps for tau ~1.4 us, doubled to ~bin 5 at 2048 points), psa_w
# wide. Both are runtime-programmable in hardware; these are just what the TB programs.
PSA_L_LO, PSA_L_HI = 1, 10
PSA_W_LO, PSA_W_HI = 1, 40

SRC = pathlib.Path("/home/ivan/datasets/clyc-FCI-test-20260828/dd_0001_scope_traces.csv")
HERE = pathlib.Path(__file__).parent


def load_traces():
    out = []
    with open(SRC, newline="") as f:
        for row in csv.reader(line for line in f if not line.startswith("#")):
            if int(row[1]) != FFT_LENGTH:
                continue
            out.append([int(x) for x in row[2:2 + FFT_LENGTH]])
    return np.array(out, dtype=np.int64)


def psa(samples):
    """Mirrors bin_accumulator.vhd: |Re| + |Im| per bin, summed over inclusive windows."""
    xk = np.fft.fft(samples.astype(np.float64), n=FFT_LENGTH)
    mag = np.abs(xk.real) + np.abs(xk.imag)
    return mag[PSA_L_LO:PSA_L_HI + 1].sum(), mag[PSA_W_LO:PSA_W_HI + 1].sum()


def main():
    traces = load_traces()
    energy = traces[:, 17:463].sum(axis=1)

    # Restricted to the instrument's real operating regime rather than the full energy range.
    # The FFT runs in 16-bit fixed point, so a frame's quantization error is relative to its own
    # amplitude: measured against this reference, a ~2.4e6-energy pulse reproduces its FCI ratio
    # to <1%, a 6e5 one to ~4%, and a 4.9e4 one only to ~12%. Those weak events are not what the
    # detector is characterized on (the Li-6 capture peak sits near 2.9e6, and events with
    # energy_long <= 0 are already discarded as invalid upstream), and including them would force
    # a tolerance so loose it could no longer catch a real bin-mapping bug -- which is the whole
    # point of this test.
    usable = np.where(energy > 1_000_000)[0]
    order = usable[np.argsort(energy[usable])]
    picks = order[np.linspace(0, len(order) - 1, N_TRACES).astype(int)]

    with open(HERE / "stimulus.txt", "w") as fs, open(HERE / "golden.txt", "w") as fg:
        fs.write(f"# {N_TRACES} real 2048-sample traces, one sample per line, decimal signed\n")
        fg.write(f"# FCI ratio (psa_l/psa_w) per trace, windows l=[{PSA_L_LO},{PSA_L_HI}] "
                 f"w=[{PSA_W_LO},{PSA_W_HI}]\n")
        for idx in picks:
            t = traces[idx]
            for s in t:
                fs.write(f"{int(s)}\n")
            l, w = psa(t)
            # Explicit decimal point: VHDL textio READ into a REAL wants a real literal, and a
            # bare integer literal is rejected by strict simulators.
            fg.write(f"{l / w:.6f}\n")

    print(f"wrote stimulus.txt ({N_TRACES} x {FFT_LENGTH} samples) and golden.txt")
    for idx in picks:
        l, w = psa(traces[idx])
        print(f"  trace {idx:3d}: energy={energy[idx]:10d}  psa_l={l:12.0f} psa_w={w:12.0f} "
              f"fci={l / w:.4f}")


if __name__ == "__main__":
    main()
