"""Appends every AcqEvent shown in the live view to a CSV file, so what the GUI displays is always
also captured for offline analysis. Header comment block then a plain CSV table, matching the
"# comments, then data" convention already used elsewhere in this project (see
data/fci_verification_set.csv's sibling scripts and the reference GUI's own log files).
"""

from __future__ import annotations

import time
from pathlib import Path

from fci_api import AcqEvent, TraceResult

CSV_HEADER = "timestamp,psa_l,psa_w,fci,energy_short,energy_long,psd"


class CsvLogger:
    def __init__(self, directory: Path, suffix: str = ""):
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix_part = f"_{suffix}" if suffix else ""
        self.path = directory / f"{stamp}_fci_live{suffix_part}.csv"

        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"# FCI-FPGA live acquisition log\n")
            f.write(f"# Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Columns: {CSV_HEADER}\n")
            f.write(f"{CSV_HEADER}\n")

        self._count = 0

    @property
    def event_count(self) -> int:
        return self._count

    def append(self, event: AcqEvent) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(
                f"{event.timestamp},{event.psa_l},{event.psa_w},{event.fci:.6f},"
                f"{event.energy_short},{event.energy_long},{event.psd:.6f}\n"
            )
        self._count += 1

    def append_many(self, events: list[AcqEvent]) -> None:
        if not events:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            for event in events:
                f.write(
                    f"{event.timestamp},{event.psa_l},{event.psa_w},{event.fci:.6f},"
                    f"{event.energy_short},{event.energy_long},{event.psd:.6f}\n"
                )
        self._count += len(events)


class TraceCsvLogger:
    """Appends one row per raw trace captured in the Trigger view: a host wall-clock
    timestamp (the device's $RT reply carries no timestamp of its own -- see TraceResult), then
    every sample in that capture. Row width varies with the "Samples" control's current setting,
    which is fine for a plain CSV -- each row is self-describing via its own sample count.

    A separate file from CsvLogger's live-event log, not another column set tacked onto it: traces
    are occasional, wide, single-shot captures for eyeballing setup, not part of the same per-event
    record the live view logs continuously.
    """

    def __init__(self, directory: Path, suffix: str = ""):
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix_part = f"_{suffix}" if suffix else ""
        self.path = directory / f"{stamp}_scope_traces{suffix_part}.csv"

        with open(self.path, "w", encoding="utf-8") as f:
            f.write("# FCI-FPGA trigger trace log\n")
            f.write(f"# Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("# Columns: host_timestamp,n_samples,sample_0,sample_1,...\n")

        self._count = 0

    @property
    def trace_count(self) -> int:
        return self._count

    def append(self, trace: TraceResult) -> None:
        samples = ",".join(str(s) for s in trace.samples)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{time.time():.6f},{len(trace.samples)},{samples}\n")
        self._count += 1
