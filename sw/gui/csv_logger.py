"""Appends every AcqEvent shown in the live view to a CSV file, so what the GUI displays is always
also captured for offline analysis. Header comment block then a plain CSV table, matching the
"# comments, then data" convention already used elsewhere in this project (see
data/fci_verification_set.csv's sibling scripts and the reference GUI's own log files).

The header also stamps the device's Trigger/PSD/FCI/BLR/Shaper settings in force when recording
started (``settings_lines``, from controllers.py's _device_settings_lines()) -- added after an
offline analysis of an earlier dataset (project log section 8j) had to reconstruct FCI/PSD from raw
traces because neither this file nor its sibling recorded what the windows/gates actually were, and
the windows had in fact changed mid-run (section 7's dd_0004 contamination, repeated). A recording
with no settings block predates this change; treat its FCI/PSD columns as untrustworthy for exactly
that reason, the same way section 8j's did.

That block also carries an `energy:` line -- the Spectrum tab's calibration coefficients, the fold
that converts `peak` into the channels they are defined against, and the formula joining them. It
is the one header entry that is not a device register: it is host state, recorded because the
`peak` column is otherwise just raw shaper counts, and the peak->keVee mapping cannot be
reconstructed from the file or from the device afterwards. A recording whose settings block has no
`energy:` line predates this; its `peak` column is still valid, but which calibration was on screen
at the time is not recoverable.
"""

from __future__ import annotations

import time
from pathlib import Path

from fci_api import AcqEvent, TraceResult

CSV_HEADER = "timestamp,psa_l,psa_w,fci,energy_short,energy_long,psd,energy,energy_cal"
"""`energy` is the pulse shaper's own raw output (AcqEvent.peak -- the shaped-pulse plateau, in
shaper counts); `energy_cal` is that same event in keVee, through the Spectrum tab's calibration.

`energy` is the column previously called `peak`, renamed in place rather than added alongside: it
holds exactly the same number, and two columns carrying identical values under different names
would be worse than one honest rename. It is still COLUMN 7, and `energy_cal` is appended as
column 8, so readers that index positionally (sw/analysis/*.py do) are unaffected by either change.

Both are written because neither substitutes for the other. The raw value is the measurement and
survives a recalibration; the calibrated one is what the event actually means, and computing it
here -- rather than leaving every reader to rediscover that it is peak/peak_fold through a
quadratic -- is the point of recording it at all. The header's `energy:` line states the mapping
joining them (controllers.py's _device_settings_lines())."""


def _write_header_prelude(f, title: str, settings_lines: list[str] | None) -> None:
    f.write(f"# {title}\n")
    f.write(f"# Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    if settings_lines:
        f.write("# Settings:\n")
        for line in settings_lines:
            f.write(f"#   {line}\n")
    else:
        f.write("# Settings: not available (not connected, or a read failed at recording start)\n")


class CsvLogger:
    """Filename is always `{prefix}_{index:04d}_fci_live.csv` -- the index is never optional (see
    controllers.py's _ensure_recording_session() for how prefix/index are chosen and why an
    unindexed name was rejected)."""

    def __init__(self, directory: Path, prefix: str, index: int,
                 settings_lines: list[str] | None = None,
                 calibration: tuple[float, float, float] = (0.0, 1.0, 0.0),
                 peak_fold: int = 1):
        """`calibration`/`peak_fold` are SNAPSHOTTED here, at recording start, and every row in the
        file is computed with them -- deliberately not tracked live. The header states one
        calibration (see _write_header_prelude's settings block), so following a mid-run edit would
        make the file's own rows disagree with its own header, and would silently put two different
        energy scales in one `energy_cal` column. Frozen, the file is internally consistent and the
        raw `energy` column is always there to recalibrate from afterwards. The defaults are the
        identity map, under which energy_cal is just the channel index."""
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{prefix}_{index:04d}_fci_live.csv"
        self._cal = calibration
        self._peak_fold = peak_fold if peak_fold > 0 else 1

        with open(self.path, "w", encoding="utf-8") as f:
            _write_header_prelude(f, "FCI-FPGA live acquisition log", settings_lines)
            f.write(f"# Columns: {CSV_HEADER}\n")
            f.write(f"{CSV_HEADER}\n")

        self._count = 0

    def _row(self, event: AcqEvent) -> str:
        c0, c1, c2 = self._cal
        ch = event.peak / self._peak_fold
        energy_cal = c0 + c1 * ch + c2 * ch * ch
        return (f"{event.timestamp},{event.psa_l},{event.psa_w},{event.fci:.6f},"
                f"{event.energy_short},{event.energy_long},{event.psd:.6f},"
                f"{event.peak},{energy_cal:.4f}\n")

    @property
    def event_count(self) -> int:
        return self._count

    def append(self, event: AcqEvent) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(self._row(event))
        self._count += 1

    def append_many(self, events: list[AcqEvent]) -> None:
        if not events:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            for event in events:
                f.write(self._row(event))
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

    def __init__(self, directory: Path, prefix: str, index: int,
                 settings_lines: list[str] | None = None):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{prefix}_{index:04d}_scope_traces.csv"

        with open(self.path, "w", encoding="utf-8") as f:
            _write_header_prelude(f, "FCI-FPGA trigger trace log", settings_lines)
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
