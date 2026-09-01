#!/usr/bin/env python3
"""Bring-up check to run straight after flashing a new bitstream + firmware.

Walks the acquisition chain in the order the signal travels and stops at the first stage that is
dead, so a failure names the stage rather than the symptom. Written after the 2026-08-31 bring-up,
where the whole chain was frozen and the visible symptom (no triggers at any threshold) pointed at
the trigger threshold while the actual fault was a stalled AXI-Stream three stages downstream.

The discriminating observation from that day, and why stage 3 checks what it does: when the
lockstep broadcaster stalls, EVERY counter reads zero -- including the error counters. Overflows
and drops sitting at zero is not "healthy", it is "nothing is moving at all". A stalled chain and
an idle one look identical unless you check whether the ADC path upstream is still alive, which
stage 1 does via blr_core's baseline.

Usage:  uv run python examples/post_flash_check.py [--seconds 10] [--threshold 400]
"""

from __future__ import annotations

import argparse
import time

from fci_api import FciClient, FciTransport, find_port

# blr_core restores the baseline to zero, so a healthy threshold is a small positive number well
# clear of the noise (sigma ~42 counts measured on this AFE) but far below the ~4000-count pulse.
DEFAULT_THRESHOLD = 400


def stage(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0, help="event-rate measurement window")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()

    port = find_port()
    if not port:
        print("FAIL: no device found")
        return 1

    failures: list[str] = []
    with FciTransport(port) as transport:
        c = FciClient(transport)
        print(f"connected to {port}: {c.identify()}")

        # --- 1. ADC -> blr_core ------------------------------------------------------------
        # A baseline that CHANGES between reads proves samples are arriving and being restored.
        # A frozen value would mean the estimator is not being clocked or fed.
        stage(1, "ADC path (blr_core baseline should be non-zero and live)")
        seen = []
        for _ in range(3):
            seen.append(c.get_blr().baseline)
            time.sleep(0.2)
        print(f"    baseline reads: {seen}")
        if all(b == 0 for b in seen):
            failures.append("blr_core baseline stuck at 0 -- no ADC data reaching the restorer")
        elif len(set(seen)) == 1:
            print("    NOTE: baseline identical across reads; live but very quiet, or held")

        # --- 2. trigger_core ---------------------------------------------------------------
        stage(2, f"Trigger (threshold={args.threshold}, depth must equal the FFT length)")
        c.reset()
        time.sleep(0.3)
        c.set_trigger(threshold=args.threshold, rising=True, delay=100, depth=2048)
        c.enable_acquisition()
        time.sleep(0.3)
        print(f"    {c.get_trigger()}")

        # --- 3. Broadcaster consumers ------------------------------------------------------
        stage(3, f"Event flow over {args.seconds:.0f} s (psd_core, fci_core, raw-trace DMA)")
        a = c.read_counts()
        time.sleep(args.seconds)
        b = c.read_counts()
        st = c.read_stats()
        d_fci = b.fci_event_count - a.fci_event_count
        d_psd = b.psd_event_count - a.psd_event_count
        print(f"    fci {d_fci:6d} events ({d_fci / args.seconds:6.1f}/s)")
        print(f"    psd {d_psd:6d} events ({d_psd / args.seconds:6.1f}/s)")
        print(f"    {st}")

        if d_fci == 0 and d_psd == 0:
            # The 2026-08-31 signature. Clean counters here mean a stalled stream, not an idle one.
            failures.append(
                "no events from either consumer. With the ADC path alive (stage 1) and all error "
                "counters clean, this is a lockstep stall on axis_broadcaster_0 -- one consumer "
                "holding TREADY low freezes the others and trigger_core never re-arms. Check "
                "system_ila_0/SLOT_2_AXIS for TVALID high with TREADY low."
            )
        elif d_fci == 0:
            failures.append("psd_core produces events but fci_core does not -- fault is in the "
                            "FCI core itself, not the shared stream")
        elif d_psd == 0:
            failures.append("fci_core produces events but psd_core does not")

        # --- 4. Raw trace ------------------------------------------------------------------
        stage(4, "Raw trace capture (axi_dma_1 S2MM)")
        tr = c.read_trace(2048)
        if tr is None:
            failures.append("read_trace returned nothing -- the raw-trace DMA never completed")
        else:
            s = tr.samples
            lo, hi = min(s), max(s)
            print(f"    {len(s)} samples, min={lo} max={hi} span={hi - lo}")
            if hi - lo < 100:
                failures.append(f"trace span {hi - lo} is noise-only -- no pulse captured")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: chain is alive end to end.")
    print("Next: collect events and compare FCI cv in the Li-6 band against PSD's ~0.10%")
    print("      (docs/log/README.md section 8g -- target is 0.8-1.6%, was 11.6-27.3%).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
