#!/usr/bin/env python3
"""Minimal, no-Qt demonstration that fci_api works standalone: connect, enable acquisition, drain
a few batches, print each event, disconnect. Run with: uv run examples/read_batch_demo.py [port]
"""

from __future__ import annotations

import sys
import time

from fci_api import FciClient, FciTransport, find_port


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    if port is None:
        print("no matching device found (VID:PID 0403:6010) -- pass a port explicitly", file=sys.stderr)
        return 1

    print(f"connecting to {port} ...")
    with FciTransport(port) as transport:
        client = FciClient(transport)

        name, major, minor = client.identify()
        print(f"identified: {name} protocol v{major}.{minor}")

        client.enable_acquisition()
        print("acquisition enabled, draining batches for 5s ...")

        t0 = time.time()
        total = 0
        while time.time() - t0 < 5.0:
            events = client.read_batch()
            total += len(events)
            for ev in events:
                print(
                    f"  ts={ev.timestamp:>14d}  fci={ev.fci:.4f}  psd={ev.psd:.4f}  "
                    f"Es={ev.energy_short:>7d}  El={ev.energy_long:>7d}"
                )
            if not events:
                time.sleep(0.1)

        print(f"total events: {total}")
        client.disable_acquisition()

    print("disconnected cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
