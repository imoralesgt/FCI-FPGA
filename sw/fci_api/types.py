"""Typed return values for fci_api, one dataclass per reply shape in docs/CLI_documentation.md.

All dataclasses are frozen: every one of them is either a read-only telemetry snapshot (AcqEvent,
Stats, TraceResult) or a config snapshot returned by a `get_*()` call, and neither should be
mutated in place -- call the matching `set_*()` to change hardware state, then `get_*()` again if
you need the new snapshot.

Two unit conventions, deliberately different, both explained where they matter:
  - AcqEvent's `fci`/`psd` are real floats (the wire's `fci_scaled / 10000`, etc.): this is
    read-only telemetry with no set direction to round-trip through, and every consumer of this
    data (plotting, CSV, the whole discrimination analysis this project is for) wants floats.
  - Config values that DO have a set direction (VgaConfig's gains) keep the documented wire unit
    (milli-units) rather than converting to floats, so a value read back and written unchanged
    round-trips exactly instead of drifting through float rounding.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AcqEvent:
    """One paired result, from `$RV` or one element of a `$RB` batch (CLI doc section 2.1/2.5)."""

    timestamp: int
    """64-bit free-running cycle counter value, combined from the wire's ts_lo/ts_hi halves."""
    psa_l: int
    psa_w: int
    fci: float
    """PSA_l / PSA_w. The wire carries this pre-scaled by 10000; already divided down here."""
    energy_short: int
    """PSD short-gate charge integral. Genuinely signed -- see project log section 8d."""
    energy_long: int
    """PSD long-gate charge integral. Genuinely signed -- see project log section 8d."""
    psd: float
    """CAEN PSD parameter, (long-short)/long. 0.0 is ambiguous: it is also the firmware's sentinel
    for "long-gate charge was <= 0, the ratio is undefined" (acquisition.c's psd_parameter_scaled).
    Check energy_long > 0 before trusting this field, exactly as firmware itself does."""
    peak: int
    """Max baseline-subtracted sample over the whole frame (raw ADC-code units), independent of the
    PSD gates -- the spectroscopy energy channel. Sent raw, not scaled like fci/psd."""


@dataclass(frozen=True, slots=True)
class Stats:
    """`$RS` -- pairing health counters (CLI doc section 2.3)."""

    paired: int
    dropped_fci: int
    dropped_psd: int
    overflow_fci: int
    overflow_psd: int
    framing_errors: int


@dataclass(frozen=True, slots=True)
class Pending:
    """`$RN` -- FIFO occupancy on both sides (CLI doc section 2.2)."""

    fci_level: int | None
    """None if the FCI result path is not present in the loaded bitstream. Not raised as
    FciNotPresentError the way read_value()/read_batch()/read_stats() are: psd_level is always
    meaningful even when fci_level is not, so this reply is only partially invalid, not wholly."""
    psd_level: int


@dataclass(frozen=True, slots=True)
class Counts:
    """`$RC` -- raw per-core event counts, independent of pairing (CLI doc section 2.4).

    Unlike Stats, these advance whether or not anything pops either FIFO. This is the field to
    poll for a live event rate; polling Stats alone while never calling read_value()/read_batch()
    observes a value that cannot move.
    """

    fci_event_count: int | None
    """None if the FCI result path is not present in the loaded bitstream -- see Pending's
    docstring for why this is a partial-absence field rather than a raised exception."""
    psd_event_count: int


@dataclass(frozen=True, slots=True)
class TraceResult:
    """`$RT` -- one captured raw trace (CLI doc section 2.6). Samples are signed."""

    samples: list[int]


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    """`$GT`/`$ST` (CLI doc section 3.1)."""

    threshold: int
    """Signed ADC code, -32768..32767."""
    rising: bool
    """True = trigger on a rising crossing (signal goes >= threshold); False = falling."""
    delay: int
    """Pre-trigger samples, 2..256."""
    depth: int
    """Capture length in samples, 1..2048."""
    cfd_fraction: int | None = None
    """Constant-fraction discriminator fraction, as value/256 (1..255). None if the device
    predates the CFD and reported only four fields."""
    cfd_delay: int | None = None
    """CFD delay in samples (1..31). Sets timing AND sensitivity: the zero crossing sits at a
    fixed n = delay/(1 - fraction) while the arming threshold is crossed later for smaller pulses,
    so pulses below roughly `threshold * rise * (1 - fraction) / cfd_delay` never arm in time and
    produce no trigger at all. A larger delay lowers that floor. None on pre-CFD firmware."""


@dataclass(frozen=True, slots=True)
class BlrConfig:
    """`$GB`/`$SB` (CLI doc section 3.2). `baseline`/`gate_open` are read-only: set_blr() has no
    parameter for them, and writing index 5 or 6 directly would get `!XX 1` from the device."""

    shift: int
    """EMA time constant = 2**shift samples, 0..15."""
    gate_thr: int
    """Estimator freezes at or above this deviation, 0..16383."""
    holdoff: int
    """Additional closed samples after the signal returns in range, 0..4095."""
    bypass: bool
    """True forwards the input unrestored."""
    hold: bool
    """True freezes the estimate."""
    baseline: int
    """Read-only: live signed estimate."""
    gate_open: bool
    """Read-only: True while the estimator is tracking."""


@dataclass(frozen=True, slots=True)
class PsdConfig:
    """`$GP`/`$SP` (CLI doc section 3.3)."""

    pre_trigger: int
    """Must equal the trigger's delay (TriggerConfig.delay) -- the core has no other way to know
    where the trigger sits inside the frame."""
    pre_gate: int
    short_gate: int
    long_gate: int
    baseline_ref: int
    """Signed pedestal trim, -32768..32767. 0 is correct when fed by the baseline restorer."""
    watermark: int
    """Interrupt threshold, 0..32. 0 disables. As of this firmware build, has no observable effect
    on GUI-visible behavior: no ISR is registered for psd_core's watermark interrupt line ($RB/$RV
    already drain the result FIFO by polling on every request instead), which is also why the
    fci_api/GUI configuration panel does not expose this field."""


@dataclass(frozen=True, slots=True)
class FciConfig:
    """`$GF`/`$SF` (CLI doc section 3.4). Bin indices address the 1024-point FFT magnitude
    spectrum; bin 512 is the Nyquist bin."""

    psa_l_lo: int
    psa_l_hi: int
    psa_w_lo: int
    psa_w_hi: int
    watermark: int | None
    """Interrupt threshold, 0..32; 0 disables. None if the FCI result path (fci_sink) is not
    present in the loaded bitstream -- that build has no watermark index at all (index 4 does not
    exist; `$SF 4`/`$GF 4` get `!XX 1` from the device), not merely an unset value. Also has no
    observable effect even where present -- see PsdConfig.watermark's docstring."""


@dataclass(frozen=True, slots=True)
class VgaConfig:
    """`$GV`/`$SV` (CLI doc section 3.5). The AD8330 gain DACs are write-only: these are the last
    values this client (or another one) has written, not a device reading."""

    fine_gain_milli: int
    """Milli-units, 1..60000; 1500 means x1.50."""
    coarse_gain_milli: int
    """Milli-units, 1..60000; 6000 means x6.00."""
    fine_dac_code: int | None
    """Raw 12-bit code, 0..4095. None until a raw code has been written this session (the wire's
    -1 sentinel for this one field, distinct from FciNotPresentError -- the VGA always exists,
    this is just "nothing written yet", not "not present in this bitstream")."""


@dataclass(frozen=True, slots=True)
class ShaperConfig:
    """`$GH`/`$SH` (CLI doc section 3.6). The shaper core is not yet in every bitstream; while
    `present` is False, values written are stored and read back but have no effect on
    acquisition."""

    present: bool
    peaking: int
    """Peaking time, in samples."""
    gap: int
    """Gap time, in samples."""
    decay: int
    """Decay / pole-zero time, in samples."""
    enable: bool
