"""Subsystem config field/panel machinery, plus the Configuration tab itself.

`SubsystemPanel` and the per-subsystem `Field` lists are the shared building block: the Trigger
panel lives inside ScopeView (scope_view.py), and the FCI/PSD panels live inside LiveView's
acquisition frames (live_view.py) -- both import from here rather than duplicating this class, per
the same one-place-per-fact reasoning as the field lists themselves. This module's own
`ConfigPanel` tab holds only the subsystems that don't have a more specific home: Baseline
Restorer, VGA, and Pulse Shaper.

Calls the config client (a RemoteFciClient, see acquisition_worker.py) directly from the GUI
thread rather than through AcquisitionWorker's request_*() methods -- safe not because of a shared
lock (there is no shared transport to lock any more: it lives only inside the reader process,
project log section 8i/8k) but because every RemoteFciClient call is its own self-contained RPC,
independently queued and answered by request id. It can freely interleave with the worker's own
streaming polls without corrupting either, the same way two independent HTTP requests don't need
to coordinate with each other. Trace/batch reads are different (they must run through the SAME
poll loop that owns pacing/adaptive-rate state, not as a one-off RPC) -- see AcquisitionWorker's
request_trace()/request_scope_start().

Built from a declarative per-field spec rather than six hand-written, nearly-identical blocks: the
whole point is that a subsystem's field list lives in exactly one place, which is the same lesson
this project already learned the hard way once (the PSD long-gate constant drifting between two
files -- project log section 8d).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from fci_api import FciClient, FciError

from .slider_spin import CycleTimeField, SliderSpinField

logger = logging.getLogger(__name__)


@dataclass
class Field:
    name: str
    """Dataclass attribute name -- also the set_*() keyword argument name."""
    label: str
    minimum: int = 0
    maximum: int = 65535
    is_bool: bool = False
    read_only: bool = False
    tooltip: str = ""
    """Labels are kept short so the slider next to them keeps most of the panel's width -- a long
    descriptive phrase used to be baked into the label itself; that detail now lives here instead,
    shown on hover rather than eating horizontal space every field row has to pay for."""
    optional: bool = False
    """True if the value can be None. Two genuinely different reasons a field is None, both
    handled here but NOT the same:
      - "not written yet this session" (VgaConfig.fine_dac_code) -- settable_when_none stays True
        (the default): the control is shown enabled at its minimum, ready to accept a first value.
      - "does not exist in this bitstream" (FciConfig.watermark) -- set settable_when_none=False:
        the control is shown disabled, because no value written through it would ever take effect
        (the device replies !XX 1 for that index on that build; see FciClient.set_fci()'s
        docstring), so offering it as editable would just be misleading.
    """
    settable_when_none: bool = True
    default: int | None = None
    """Value the control shows before any device value has been read. None means "use minimum",
    which is right for most fields. Set it wherever the minimum is NOT a usable setting -- the CFD
    fields are the case in point: their minima are protocol bounds, while the value that actually
    works is the one firmware boots with (bringup.c's CFD_FRACTION / CFD_DELAY). Showing 1/256 and
    a 1-sample delay implied a configuration that would trigger on almost nothing."""
    mirrors: str | None = None
    """Name of another field in the same list whose control also drives this one. A mirrored field
    gets NO control of its own -- it is written, read back and project-saved as a full field, but
    its value always comes from the named field's widget, so the panel can only ever produce one
    value for the pair.

    Used for FCI's psa_l_lo/psa_w_lo. Note this is a constraint of the METHOD, not of the hardware:
    fci_axi4lite_regs.vhd really does have two independent registers (0x00 psa_l_lo, 0x08
    psa_w_lo), each driving its own comparison in bin_accumulator.vhd, and the CLI can still set
    them independently over $SF. But FCI is the ratio of a narrow band to a wide one that CONTAINS
    it, so a lower edge that differs between them is not a different tuning of the index, it is a
    different quantity -- and every result in this project (docs/log/README.md's parameter table,
    the sweep in sw/analysis/sweep_cosmics_gates.py, which sweeps one shared `lo`) assumes the
    shared edge. Two spin boxes invited a combination none of that analysis covers."""
    cycle_period_ns: float | None = None
    """Set on a field whose wire value is a clock cycle count but whose natural human unit is
    time (pulse_shaper_core's peaking/flat_top/decay, at 20 ns/cycle @ 50 Msps). When set, the
    control is a CycleTimeField instead of a SliderSpinField: a microsecond-labelled spin box that
    only ever lands on exact multiples of this period, stepping by one cycle at a time. The
    dataclass value stays a plain integer cycle count either way -- this only changes what the
    control DISPLAYS, matching the module's config values keep the wire unit convention (see
    fci_api/types.py's own module docstring)."""


class SubsystemPanel(QGroupBox):
    """One subsystem's Refresh/Apply form. Owns its own client reference (set_client()) rather
    than being handed bound get/set callables by a parent container -- this is what lets the same
    class live inside ConfigPanel, LiveView, or ScopeView interchangeably, each just calling
    set_client() on whichever panels it holds when a connection comes up or drops.
    """

    config_changed = Signal(object)
    """Emitted with the freshly read dataclass on every successful refresh() (on connect, on the
    user's own Refresh click, and again after a successful Apply). Lets an embedding view (e.g.
    ScopeView's trigger dashed line) stay in sync without polling this panel itself."""

    def __init__(self, title: str, fields: list[Field], get_name: str, set_name: str):
        super().__init__(title)
        self._fields = fields
        self._get_name = get_name
        self._set_name = set_name
        self.key = get_name.removeprefix("get_")
        """Stable subsystem identifier ("trigger", "psd", "fci", "blr", "vga", "shaper") -- the key
        a project's settings.json stores this panel's values under (project.py). Derived from the
        accessor name rather than passed in at construction: every panel already names its getter,
        so there is no second place for the two to drift apart, and no construction site needs
        touching to gain one."""
        self._client: FciClient | None = None
        self._controls: dict[str, QWidget] = {}
        self._last: Any = None
        self._shown_when_none: dict[str, Any] = {}
        """For optional fields the device reported as None: the placeholder value refresh() put in
        the widget. apply() diffs against this so an untouched placeholder is never written back --
        see apply() for the hardware damage that caused."""

        grid = QGridLayout(self)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        row = -1
        for f in fields:
            if f.mirrors is not None:
                # No control of its own: _widget_for() routes it to the field it mirrors, which is
                # the whole point -- one widget, so the pair cannot be given two values here.
                continue
            row += 1
            lbl = QLabel(f.label + ":")
            if f.tooltip:
                lbl.setToolTip(f.tooltip)
            grid.addWidget(lbl, row, 0)
            if f.is_bool:
                w = QCheckBox()
            elif f.cycle_period_ns is not None:
                w = CycleTimeField(f.minimum, f.maximum, f.cycle_period_ns)
                if f.default is not None:
                    w.setValue(f.default)
            else:
                w = SliderSpinField(f.minimum, f.maximum)
                if f.default is not None:
                    w.setValue(f.default)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            w.setEnabled(not f.read_only)
            grid.addWidget(w, row, 1)
            self._controls[f.name] = w

        btn_row = row + 1  # rows actually added, which is fewer than len(fields) when any mirror
        self.btn_refresh = QPushButton("Refresh")
        self.btn_apply = QPushButton("Apply")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_apply.clicked.connect(self.apply)
        grid.addWidget(self.btn_refresh, btn_row, 0)
        grid.addWidget(self.btn_apply, btn_row, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #c0392b;")
        grid.addWidget(self.lbl_status, btn_row + 1, 0, 1, 2)

        self.set_controls_enabled(False)

    def set_client(self, client: FciClient | None) -> None:
        self._client = client
        self.set_controls_enabled(client is not None)
        if client is not None:
            self.refresh()

    def _widget_for(self, f: Field):
        """The control that holds `f`'s value -- its own, or the one it mirrors (Field.mirrors).
        Every value path goes through here, which is what keeps a mirrored field a real field
        everywhere (applied, project-saved, range-checked) while still having exactly one widget
        behind it."""
        return self._controls[f.mirrors or f.name]

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_refresh.setEnabled(enabled)
        self.btn_apply.setEnabled(enabled)
        for name, w in self._controls.items():
            read_only = next((f.read_only for f in self._fields if f.name == name), False)
            w.setEnabled(enabled and not read_only)

    def refresh(self) -> None:
        if self._client is None:
            return
        try:
            cfg = getattr(self._client, self._get_name)()
        except FciError as e:
            # Non-blocking by design: refresh() runs automatically on every connect (via
            # set_client()), not only from this panel's own button, so a transient failure here
            # must never be able to stop and wait for a user click -- a modal QMessageBox in this
            # path previously froze the whole application on exactly that sequence (a read failing
            # during the automatic post-connect refresh).
            logger.warning(f"{self.title()}: refresh failed: {e}")
            self.lbl_status.setText(f"Read failed: {e}")
            return
        self.lbl_status.setText("")
        self._last = cfg
        for f in self._fields:
            value = getattr(cfg, f.name)
            if f.mirrors is not None:
                # Displayed by the field it mirrors; nothing to populate here. Do surface a device
                # state this panel cannot represent, though, rather than hiding it: the CLI and the
                # RTL both allow the pair to differ (see Field.mirrors), so a value set outside
                # this GUI can legitimately arrive split, and silently showing only one of the two
                # would misreport what the device is actually running.
                source = getattr(cfg, f.mirrors, None)
                if value is not None and source is not None and value != source:
                    logger.warning(f"{self.title()}: device has {f.mirrors}={source} but "
                                   f"{f.name}={value}; this panel shows one control for both and "
                                   f"an Apply will set {f.name} to {source}")
                    self.lbl_status.setText(f"Device has {f.name}={value}, {f.mirrors}={source}; "
                                            f"Apply will set both to {source}")
                continue
            w = self._controls[f.name]
            if f.optional and value is None:
                if f.settable_when_none:
                    # Not written yet this session, but a first value CAN be applied -- leave the
                    # control enabled at a sane default rather than locking it out.
                    w.setEnabled(not f.read_only)
                    if f.is_bool:
                        w.setChecked(False)
                        self._shown_when_none[f.name] = False
                    else:
                        shown = f.default if f.default is not None else f.minimum
                        w.setValue(shown)
                        # Remember what we PUT here, so apply() can tell "the user deliberately
                        # chose this" from "this is just the placeholder we displayed". See
                        # apply()'s use of _shown_when_none.
                        self._shown_when_none[f.name] = shown
                else:
                    # Does not exist in this bitstream; no value written here would ever apply.
                    w.setEnabled(False)
                continue
            if value is None:
                # Reached only when a field the device did not report is NOT marked optional --
                # a host/firmware schema mismatch, not a user error. Warn and skip rather than
                # raise: this runs on every connect, and int(None) here previously escaped as an
                # unhandled TypeError that killed the application before the window was usable.
                logger.warning(f"{self.title()}: device did not report '{f.name}'; "
                               "mark the Field optional= if this bitstream legitimately lacks it")
                w.setEnabled(False)
                continue
            w.setEnabled(not f.read_only)
            if f.is_bool:
                w.setChecked(bool(value))
            else:
                w.setValue(int(value))
        self.config_changed.emit(cfg)

    # ------------------------------------------------------------------- project save/restore

    def get_values(self) -> dict[str, int | bool]:
        """This panel's writable fields, as they currently stand in the CONTROLS -- for a project
        to store (project.py). The controls, not `self._last`: what the user sees is what a Save is
        expected to capture, and after any successful Apply the two agree anyway (apply() refreshes).

        Read-only fields are excluded because writing them back is meaningless. Optional fields the
        device reported as None are excluded unless the user has actually moved them, for the same
        reason apply() skips them: VgaConfig.fine_dac_code is a raw override of the same DAC channel
        as fine_gain_milli, written last, so capturing its placeholder into a project and applying
        that project later would silently zero the fine gain -- the hardware failure documented in
        apply() below. When no device value has ever been read (`_last` is None, e.g. saving while
        disconnected) every optional field is skipped, since there is then no way to tell a
        deliberate value from a placeholder.
        """
        values: dict[str, int | bool] = {}
        for f in self._fields:
            if f.read_only:
                continue
            # Mirrored fields are saved too, at the mirrored value -- a project file stays a
            # complete description of the device's registers, with the same keys as before.
            w = self._widget_for(f)
            current = w.isChecked() if f.is_bool else w.value()
            if f.optional:
                if self._last is None or not f.settable_when_none:
                    continue
                if getattr(self._last, f.name) is None and \
                        current == self._shown_when_none.get(f.name, current):
                    continue
            values[f.name] = current
        return values

    def set_values(self, values: dict[str, Any]) -> None:
        """Loads project-stored values INTO the controls. Does not write to the device -- that is
        apply()'s job, and keeping the two separate is what lets a project be opened while
        disconnected (or opened and inspected before deciding to push it to hardware).

        Unknown keys are logged and ignored rather than raising: a project written by a build with
        an extra field must still open here, and the field lists are the authority on what this
        build has.
        """
        for name, value in values.items():
            field = next((f for f in self._fields if f.name == name), None)
            if field is None:
                logger.warning(f"{self.title()}: project sets unknown field '{name}'; ignored")
                continue
            if field.read_only:
                continue
            if field.mirrors is not None:
                # The field it mirrors carries the pair's value into the one shared control, so
                # loading this one too would just overwrite it with the same number -- or, for a
                # project saved before the two were tied, with a DIFFERENT one, silently letting
                # key order decide which wins. Warn on that case and keep the source field's.
                source = values.get(field.mirrors)
                if source is not None and int(value) != int(source):
                    logger.warning(f"{self.title()}: project has {field.mirrors}={source} but "
                                   f"{name}={value}; loading {source} for both (see Field.mirrors)")
                continue
            w = self._controls[name]
            if field.is_bool:
                w.setChecked(bool(value))
            else:
                w.setValue(int(value))

    def apply(self) -> None:
        if self._last is None:
            self.refresh()
            if self._last is None:
                return

        kwargs = {}
        for f in self._fields:
            if f.read_only:
                continue
            old_value = getattr(self._last, f.name)
            if f.optional and old_value is None:
                if not f.settable_when_none:
                    continue  # genuinely absent in this bitstream -- see Field.optional's docstring
                # No device-side baseline to diff against, so diff against the placeholder
                # refresh() displayed instead: send this only if the user actually moved it.
                #
                # This used to unconditionally include the field ("first value ever, always send
                # it"), which was actively destructive for VgaConfig.fine_dac_code: that field is a
                # RAW override of the same physical DAC channel as fine_gain_milli (both are
                # command 0x31, "DAC A" -- see vga_dac.c), and it is written LAST, so every VGA
                # Apply silently clobbered the fine gain the user had just set with a raw code of
                # 0, i.e. essentially zero gain. On hardware that read as "event rate drops to zero
                # after any VGA change, whatever value you set". Found 2026-09-03.
                w = self._widget_for(f)
                current = w.isChecked() if f.is_bool else w.value()
                if current != self._shown_when_none.get(f.name, current):
                    kwargs[f.name] = current
                continue
            # _widget_for, so a mirrored field is still diffed against ITS OWN device value and
            # written when they differ -- which is what pulls a device found with a split pair
            # back into agreement on the next Apply.
            w = self._widget_for(f)
            new_value = w.isChecked() if f.is_bool else w.value()
            if new_value != old_value:
                kwargs[f.name] = new_value

        if not kwargs:
            return
        try:
            getattr(self._client, self._set_name)(**kwargs)
        except FciError as e:
            logger.warning(f"{self.title()}: apply failed: {e}")
            self.lbl_status.setText(f"Write failed: {e}")
        self.refresh()


TRIGGER_FIELDS = [
    Field("threshold", "Threshold", -32768, 32767,
          tooltip="Signed ADC code that ARMS the discriminator. It decides whether an event is "
                  "real; the CFD zero crossing decides when it happened."),
    Field("rising", "Rising edge", is_bool=True,
          tooltip="Trigger on the signal crossing threshold upward (checked) or downward."),
    Field("delay", "Delay (samples)", 4, 256,
          tooltip="Pre-trigger delay. Minimum 4: the CFD pipeline is ~3 samples deep, so below "
                  "that the trigger point falls outside the captured window. Kept in sync with "
                  "PSD's Pre-trigger automatically."),
    Field("depth", "Depth (samples)", 1, 2048,
          tooltip="Capture length, also the window FCI and PSD see. Safe to change while running: "
                  "sample_framer owns the FFT's frame boundary and zero-pads a short capture up "
                  "to 2048, so FCI stays a well-defined transform of the samples that arrived "
                  "(and gets QUIETER, since padding zeros carry no noise). Keep PSD's pre-gate + "
                  "long gate inside this length, or those integrals run off the end of the "
                  "trace."),
    # optional/settable_when_none=False: pre-CFD firmware answers $GT with four fields, so
    # get_trigger() reports these two as None (deliberately tolerated rather than raising, so a
    # host can still drive an older bitstream). Without the flags, refresh() reached int(None) and
    # crashed the whole GUI on connect, and apply() then wrote '$ST 4 1' -- an index that firmware
    # does not have -- from the widget's untouched minimum. Both controls light up on their own
    # once a CFD build is flashed and $GT starts answering with six.
    # Ranges and defaults track cli.c's own clamps and bringup.c's CFD_FRACTION / CFD_DELAY --
    # the register reset values are the same pair, so all three agree by construction.
    Field("cfd_fraction", "CFD fraction (/256)", 1, 255, optional=True, settable_when_none=False,
          default=64,
          tooltip="Constant-fraction discriminator attenuation, as fraction/256. Default 64 = 1/4, "
                  "matching the register reset. With the delay below it, sets the zero crossing "
                  "at n = delay / (1 - fraction/256). Disabled if this bitstream predates the CFD "
                  "trigger."),
    Field("cfd_delay", "CFD delay (samples)", 4, 31, optional=True, settable_when_none=False,
          default=24,
          tooltip="CFD delay. Sets SENSITIVITY as well as timing: pulses smaller than about "
                  "threshold x rise x (1 - fraction) / delay never arm in time and produce no "
                  "trigger at all, silently. A larger delay lowers that floor -- the default 24 "
                  "puts it near 1.25x threshold; 8 would put it at 3.75x. Minimum 4, for the same "
                  "reason as the pre-trigger Delay: below that the crossing at n = delay/(1-f) "
                  "falls inside the CFD's own ~3-sample pipeline. Disabled if this bitstream "
                  "predates the CFD trigger."),
]

BLR_FIELDS = [
    Field("shift", "Shift k", 0, 15, tooltip="Baseline restorer time constant: tau = 2^k samples."),
    Field("gate_thr", "Gate threshold", 0, 16383,
          tooltip="Deviation from baseline (counts) that opens the restorer's gate."),
    Field("holdoff", "Hold-off", 0, 4095,
          tooltip="Samples to hold the gate closed after a pulse (samples)."),
    Field("bypass", "Bypass", is_bool=True,
          tooltip="Disable baseline restoration entirely. WARNING: this also stops the CFD "
                  "trigger working. The discriminator needs a zero-centred baseline -- at a "
                  "resting level b the bipolar signal sits at b(1-fraction) and never crosses "
                  "zero -- so with the restorer bypassed there are no triggers at all."),
    Field("hold", "Hold", is_bool=True, tooltip="Freeze the baseline estimate."),
    Field("baseline", "Baseline (RO)", -32768, 32767, read_only=True,
          tooltip="Live baseline estimate (read-only)."),
    Field("gate_open", "Gate open (RO)", is_bool=True, read_only=True,
          tooltip="Live gate-open flag (read-only)."),
]

TRACE_MAX_SAMPLES = 2048
"""Matches the Trigger's own Depth field's hardware max (trigger_core's MAX_DEPTH). A gate can
never usefully extend past the captured trace itself, so this -- not the register's raw 16-bit
width -- is the field's real ceiling."""

PSD_FIELDS = [
    Field("pre_trigger", "Pre-trigger", 0, TRACE_MAX_SAMPLES,
          tooltip="Must equal the Trigger tab's Delay -- kept in sync automatically."),
    Field("pre_gate", "Pre-gate", 0, TRACE_MAX_SAMPLES,
          tooltip="Samples before the trigger where gate integration begins. Cannot exceed the "
                  "trace length."),
    Field("short_gate", "Short gate", 0, TRACE_MAX_SAMPLES,
          tooltip="Short-gate integration length (samples). Cannot exceed the trace length."),
    Field("long_gate", "Long gate", 0, TRACE_MAX_SAMPLES,
          tooltip="Long-gate integration length (samples); should cover the full pulse. Cannot "
                  "exceed the trace length."),
    Field("baseline_ref", "Baseline ref", -32768, 32767,
          tooltip="Signed pedestal trim; 0 when fed by the baseline restorer."),
    # Watermark deliberately not exposed here: this firmware build's ISR table has no handler for
    # its interrupt line, and $RB/$RV already drain the FIFO by polling on every request, so the
    # field has no observable effect -- see PsdConfig.watermark's docstring in fci_api/types.py.
]

FCI_FIELDS = [
    # Upper bound is the Nyquist bin of the 2048-point transform. Bin spacing is 50 Msps / 2048 =
    # ~24.4 kHz, so bin k is k * 24.4 kHz.
    Field("psa_l_lo", "Low bin (both)", 0, 1024,
          tooltip="Shared low FFT bin index for BOTH bands (~24.4 kHz per bin). One control, "
                  "because FCI is the ratio of a narrow band to a wide band that contains it -- "
                  "the two bands share a lower edge by construction, and every tuning result in "
                  "this project assumes that. Applied to psa_l_lo and psa_w_lo alike."),
    Field("psa_l_hi", "PSA_l high", 0, 1024, tooltip="PSA_l high FFT bin index (~24.4 kHz per bin)."),
    Field("psa_w_lo", "PSA_w low", 0, 1024, mirrors="psa_l_lo",
          tooltip="Mirrors the shared low bin; not separately settable here."),
    Field("psa_w_hi", "PSA_w high", 0, 1024, tooltip="PSA_w high FFT bin index (~24.4 kHz per bin)."),
    # Watermark deliberately not exposed here -- same reasoning as PSD_FIELDS above.
]

VGA_FIELDS = [
    Field("fine_gain_milli", "Fine gain", 1, 60000, tooltip="Milli-units; 1500 = x1.50."),
    Field("coarse_gain_milli", "Coarse gain", 1, 60000, tooltip="Milli-units; 6000 = x6.00."),
    Field("fine_dac_code", "Fine DAC code", 0, 4095, optional=True, tooltip="Raw DAC code."),
]

SHAPER_CYCLE_NS = 20.0  # 1 clock period @ 50 Msps

SHAPER_FIELDS = [
    Field("peaking", "Peaking time", 10, 256, cycle_period_ns=SHAPER_CYCLE_NS,
          tooltip="Peaking (rise) time. Should sit a bit past the detector's own physical rise "
                  "time so the trapezoid's ramp fully captures it. The flat-top plateau height "
                  "scales with this value; changing it after calibrating requires recalibrating."),
    Field("flat_top", "Flat-top", 0, 256, cycle_period_ns=SHAPER_CYCLE_NS,
          tooltip="Flat-top length. 0 is a valid \"triangular, no plateau\" configuration. Longer "
                  "averages more samples (better noise rejection) at the cost of more dead time "
                  "per pulse."),
    Field("decay", "Decay (pole-zero)", 2, 300, cycle_period_ns=SHAPER_CYCLE_NS,
          tooltip="Pole-zero decay time constant. Match this to the detector's own measured pulse "
                  "decay tau -- for a matched value the flat-top plateau is exactly flat, "
                  "independent of the pulse's true decay; a mismatch shows up as a slope or "
                  "under/overshoot on the plateau instead."),
    Field("enable", "Enable", is_bool=True,
          tooltip="Off bypasses shaping entirely: the raw single-sample peak is reported instead, "
                  "useful as an A/B reference against the shaped amplitude."),
]


class ConfigPanel(QWidget):
    """The subsystems with no more specific home: Baseline Restorer, VGA, Pulse Shaper. Trigger
    lives in ScopeView and FCI/PSD live in LiveView, each right beside the view their parameters
    actually affect -- see this module's own docstring.

    Constructed with no client (MainWindow builds all tabs upfront, disabled, before any
    connection exists -- matching the reference GUI's pattern). Call set_client() once a
    connection is established.
    """

    def __init__(self):
        super().__init__()
        self.panels: list[SubsystemPanel] = []

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)

        specs = [
            ("Baseline Restorer", BLR_FIELDS, "get_blr", "set_blr"),
            ("VGA", VGA_FIELDS, "get_vga", "set_vga"),
            ("Pulse Shaper (may be absent from this bitstream)", SHAPER_FIELDS,
             "get_shaper", "set_shaper"),
        ]
        for title, fields, get_name, set_name in specs:
            panel = SubsystemPanel(title, fields, get_name, set_name)
            self.panels.append(panel)
            inner_layout.addWidget(panel)
        inner_layout.addStretch(1)

        self.shaper_config = next(p for p in self.panels if p.key == "shaper")
        """Named accessor for the one panel another view has to reach into: HistogramView needs
        `peaking` to know what a spectrum channel means (main_window.py wires config_changed to
        it). Matches how LiveView/ScopeView expose psd_config/trigger_config for their own
        cross-tab syncs, rather than making callers index self.panels positionally."""

        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def set_client(self, client: FciClient | None) -> None:
        for panel in self.panels:
            panel.set_client(client)

    def set_controls_enabled(self, enabled: bool) -> None:
        for panel in self.panels:
            panel.set_controls_enabled(enabled)
