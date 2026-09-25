"""Single-point detector characterization, for stepping PMT high voltage (or any gain change).

Run it once per HV setting. It captures raw traces live over the serial link and reports the four
numbers that actually decide whether the setting is usable:

  1. baseline sigma          -- the electronic noise floor, measured in a quiet region of the trace
  2. peak amplitude spread   -- median / p95 / max, against the saturation ceiling
  3. clipping fraction       -- what fraction of events are already flat-topped
  4. per-event tail SNR      -- delayed-component integral over its own noise

(4) is the one that matters for an organic scintillator. All of the n/gamma shape information in an
organic lives in the delayed component, so if the tail integral is below the per-event noise then no
choice of PSD gates or FCI bins can recover discrimination -- the information is simply not in the
digitized record. Raising PMT gain fixes that and raising VGA gain mostly does not, because the HV
acts before the AFE adds its own noise while the VGA acts after it.

The saturation ceiling is NOT the ADC's 14-bit full scale. It is the measured analog ceiling of this
front end (see the `measured-pulse-shape` note: ~12600 counts at VGA 2.0x, and the top of a
saturated pulse is shaped rather than hard-clipped). That is why clipping is detected by looking for
flat tops rather than by comparing against a rail.

Usage:
    python characterize_detector.py --label "HV 900V" --n 200
    python characterize_detector.py --label "HV 950V" --n 200 --save hv950.npy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fci_api.client import FciClient  # noqa: E402
from fci_api.transport import FciTransport, find_port  # noqa: E402

SAT_COUNTS = 12600.0
"""Measured analog saturation of this front end, not the ADC full scale -- see module docstring.

Used only as the headroom reference. Actual clipping is detected from the data by `find_rail()`,
because which ceiling binds depends on gain: on the CLYC+SiPM setup the analog ceiling above bound
first, while the OGS+PMT setup at -1200 V rails digitally at ~14562 counts, which is the ADC's
14-bit full scale (16383) less the ~1821-count baseline the BLR removes."""

ADC_RAIL = 14562.0
"""Digital ceiling: 14-bit full scale (16383) minus the baseline the BLR subtracts. This is the one
that binds at PMT gains, and it is what headroom is judged against."""


def find_rail(traces: np.ndarray, min_frac: float = 0.01) -> float | None:
    """Detect a hard ceiling as a spike of events sharing one exact peak value.

    A flat-top test is the obvious way to find clipping and it does NOT work on this detector: the
    OGS prompt pulse is only ~3 samples wide at 50 Msps, so a clipped event typically has a SINGLE
    sample at the rail and looks like a perfectly ordinary peak. Requiring 3 samples at the maximum
    reported 0.7% clipping on a population that was actually 39% clipped. Peak VALUE, not peak
    shape, is what gives it away -- a rail makes many events report bit-identical maxima, which is
    vanishingly unlikely otherwise.
    """
    peaks = traces.max(axis=1)
    values, counts = np.unique(peaks, return_counts=True)
    top = counts.argmax()
    if counts[top] >= max(2, min_frac * len(peaks)) and values[top] > 0.5 * peaks.max():
        return float(values[top])
    return None

QUIET_LO, QUIET_HI = 150, 250
"""Sample range used for the baseline-noise estimate. Deliberately far from the trigger: the first
tens of samples contain the pulse itself, and measuring 'baseline' noise across them inflates sigma
by several times (that mistake was made once on this detector and read as a noisy front end)."""

PROMPT_SAMPLES = 4
"""Prompt integration width. The OGS prompt component is ~3 samples wide at 50 Msps; 4 covers it
with one sample of trigger jitter margin."""

TAIL_END = 100
"""End of the delayed-component integral, in samples after the peak (2 us at 50 Msps)."""


def capture(client: FciClient, n: int, depth: int) -> np.ndarray:
    traces = []
    misses = 0
    while len(traces) < n and misses < n * 4:
        try:
            tr = client.read_trace(depth)
        except Exception:
            misses += 1
            continue
        if tr is None:
            misses += 1
            continue
        traces.append(np.asarray(tr.samples, dtype=float))
    return np.array(traces)


def analyze(traces: np.ndarray, label: str) -> dict:
    if len(traces) == 0:
        raise SystemExit("no traces captured -- is the detector triggering?")

    sigma = float(traces[:, QUIET_LO:QUIET_HI].std())
    peaks = traces.max(axis=1)

    rail = find_rail(traces)
    clipped = int((peaks >= rail - sigma).sum()) if rail is not None else 0

    aligned = []
    for s in traces:
        i = int(np.argmax(s))
        if 10 <= i <= len(s) - TAIL_END:
            aligned.append(s[i - 10:i + TAIL_END])

    prompt = tail = tail_snr = float("nan")
    if aligned:
        A = np.array(aligned)
        # Clipped events must be excluded here, not just counted. A railed prompt is truncated
        # while its tail is untouched, so leaving them in inflates the tail/prompt ratio and
        # flatters the very number this tool exists to report.
        if rail is not None:
            unclipped = A[A.max(axis=1) < rail - sigma]
            if len(unclipped) >= 10:
                A = unclipped
        prompt = float(A[:, 10:10 + PROMPT_SAMPLES].mean(axis=0).sum())
        tail_per_event = A[:, 10 + PROMPT_SAMPLES:].sum(axis=1)
        tail = float(tail_per_event.mean())
        n_tail = A.shape[1] - 10 - PROMPT_SAMPLES
        tail_snr = tail / (sigma * np.sqrt(n_tail))

    return {
        "label": label,
        "n": len(traces),
        "rail": rail,
        "n_clean": len(A) if aligned else 0,
        "sigma": sigma,
        "p50": float(np.median(peaks)),
        "p95": float(np.percentile(peaks, 95)),
        "p99": float(np.percentile(peaks, 99)),
        "max": float(peaks.max()),
        # Headroom is judged on p99 against the ADC rail, not on p95 against the analog ceiling.
        # p95 is too forgiving: at -1000 V it left 1.5x of apparent headroom and advised raising HV
        # again, when p99 was already within 1.4x of the rail and the next step would have clipped.
        "headroom": ADC_RAIL / max(float(np.percentile(peaks, 99)), 1.0),
        "clip_pct": 100.0 * clipped / len(traces),
        "prompt": prompt,
        "tail": tail,
        "tail_frac": tail / prompt if prompt else float("nan"),
        "tail_snr": tail_snr,
    }


def report(r: dict) -> None:
    print(f"\n=== {r['label']}  ({r['n']} traces) ===")
    print(f"  baseline sigma      {r['sigma']:8.1f} counts")
    print(f"  peak p50/p95/p99    {r['p50']:7.0f} / {r['p95']:.0f} / {r['p99']:.0f} counts")
    print(f"  headroom to rail    {r['headroom']:8.1f}x  (on p99, rail {ADC_RAIL:.0f})")
    rail_s = f"{r['rail']:.0f}" if r["rail"] is not None else "none detected"
    print(f"  hard rail at        {rail_s:>8} counts")
    print(f"  CLIPPED events      {r['clip_pct']:8.1f} %   ({r['n_clean']} clean events used below)")
    print(f"  prompt integral     {r['prompt']:8.0f} counts")
    print(f"  tail integral       {r['tail']:8.0f} counts  ({100 * r['tail_frac']:.1f}% of prompt)")
    print(f"  PER-EVENT TAIL SNR  {r['tail_snr']:8.2f}   <-- the number that decides PSD/FCI")

    print("  verdict: ", end="")
    if r["clip_pct"] > 2.0:
        drop = max(1.5, r["rail"] / max(r["p50"], 1.0)) if r["rail"] else 2.0
        print(f"CLIPPING at {r['clip_pct']:.0f}% -- energy AND PSD are wrong for those events.")
        print(f"           back HV off by ~{drop:.1f}x in gain "
              f"(= divide voltage by {drop ** (1 / 5.0):.2f}, alpha~5 measured on this tube).")
    elif r["tail_snr"] < 1.5:
        print("delayed component still under the noise; raise HV further.")
    elif r["headroom"] < 1.5:
        print("tail is usable but headroom is nearly gone; this is about the limit.")
    else:
        print("usable, and there is still headroom -- one more step is worth trying.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="unlabeled", help="e.g. 'HV 900V' -- printed in the report")
    ap.add_argument("--n", type=int, default=200, help="traces to capture")
    ap.add_argument("--depth", type=int, default=256, help="samples per trace")
    ap.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    ap.add_argument("--save", default=None, help="write the raw traces to this .npy file")
    args = ap.parse_args()

    port = args.port or find_port() or "/dev/ttyUSB2"
    transport = FciTransport(port)
    transport.open()
    try:
        client = FciClient(transport)
        print(f"port {port}")
        print(f"shaper  {client.get_shaper()}")
        print(f"vga     {client.get_vga()}")
        traces = capture(client, args.n, args.depth)
    finally:
        transport.close()

    if args.save:
        np.save(args.save, traces)
        print(f"saved {len(traces)} traces to {args.save}")

    report(analyze(traces, args.label))


if __name__ == "__main__":
    main()
