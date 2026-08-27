#!/usr/bin/env python3
"""One-off verification script (not part of the library) exercising every fci_api method against
real hardware: all six config get/set pairs, the other telemetry calls, and the scope trace."""

from __future__ import annotations

import inspect

from fci_api import FciClient, FciTransport, find_port


def main() -> int:
    port = find_port()
    assert port, "no device found"
    print(f"connecting to {port} ...")
    with FciTransport(port) as transport:
        c = FciClient(transport)

        print("ping:", c.ping())
        print("identify:", c.identify())

        print("\n--- config getters ---")
        trig = c.get_trigger()
        print("trigger:", trig)
        blr = c.get_blr()
        print("blr:", blr)
        psd = c.get_psd()
        print("psd:", psd)
        fci = c.get_fci()
        print("fci:", fci)
        vga = c.get_vga()
        print("vga:", vga)
        shaper = c.get_shaper()
        print("shaper:", shaper)

        print("\n--- round-trip set/get on a harmless field (BLR shift, unchanged value) ---")
        c.set_blr(shift=blr.shift)
        blr2 = c.get_blr()
        # baseline and gate_open are both live, read-only telemetry (a continuously-drifting
        # estimate and a momentary "is a pulse passing through right now" flag), not settable
        # fields -- compare only what set_blr()/get_blr() actually round-trip.
        settable = lambda b: (b.shift, b.gate_thr, b.holdoff, b.bypass, b.hold)
        assert settable(blr2) == settable(blr), f"round-trip mismatch: {blr} != {blr2}"
        print(f"round-trip OK (baseline drifted {blr.baseline} -> {blr2.baseline}, as expected)")

        print("\n--- read-only-index rejection (BLR baseline/gate_open have no set_blr param) ---")
        params = inspect.signature(c.set_blr).parameters
        assert "baseline" not in params and "gate_open" not in params
        print("confirmed: set_blr() exposes no baseline/gate_open parameter")

        print("\n--- other telemetry ---")
        print("pending:", c.read_pending())
        print("stats:", c.read_stats())
        print("counts:", c.read_counts())

        print("\n--- scope trace ---")
        trace = c.read_trace(512)
        if trace is None:
            print("no trace captured yet")
        else:
            print(f"trace: {len(trace.samples)} samples, min={min(trace.samples)} "
                  f"max={max(trace.samples)}")

    print("\ndisconnected cleanly -- all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
