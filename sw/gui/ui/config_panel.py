"""Subsystem config field/panel machinery, plus the Configuration tab itself.

`SubsystemPanel` and the per-subsystem `Field` lists are the shared building block: the Trigger
panel lives inside ScopeView (scope_view.py), and the FCI/PSD panels live inside LiveView's
acquisition frames (live_view.py) -- both import from here rather than duplicating this class, per
the same one-place-per-fact reasoning as the field lists themselves. This module's own
`ConfigPanel` tab holds only the subsystems that don't have a more specific home: Baseline
Restorer, VGA, and Pulse Shaper.

Calls fci_api directly from the GUI thread (not routed through AcquisitionWorker) -- this is the
one deliberate exception the approved plan calls out: FciTransport's RLock is exactly what makes
this safe alongside the worker thread's concurrent read_batch() polling. Trace/batch reads are
different (they must run ON the worker thread itself, not just under the same lock) -- see
AcquisitionWorker's docstring for why.

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

from .slider_spin import SliderSpinField

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
        self._client: FciClient | None = None
        self._controls: dict[str, QWidget] = {}
        self._last: Any = None

        grid = QGridLayout(self)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        for row, f in enumerate(fields):
            lbl = QLabel(f.label + ":")
            if f.tooltip:
                lbl.setToolTip(f.tooltip)
            grid.addWidget(lbl, row, 0)
            if f.is_bool:
                w = QCheckBox()
            else:
                w = SliderSpinField(f.minimum, f.maximum)
                if f.default is not None:
                    w.setValue(f.default)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            w.setEnabled(not f.read_only)
            grid.addWidget(w, row, 1)
            self._controls[f.name] = w

        btn_row = len(fields)
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

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_refresh.setEnabled(enabled)
        self.btn_apply.setEnabled(enabled)
        for f in self._fields:
            self._controls[f.name].setEnabled(enabled and not f.read_only)

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
            w = self._controls[f.name]
            if f.optional and value is None:
                if f.settable_when_none:
                    # Not written yet this session, but a first value CAN be applied -- leave the
                    # control enabled at a sane default rather than locking it out.
                    w.setEnabled(not f.read_only)
                    if f.is_bool:
                        w.setChecked(False)
                    else:
                        w.setValue(f.default if f.default is not None else f.minimum)
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
                # First value ever for this field: no baseline to diff against, always include it.
                w = self._controls[f.name]
                kwargs[f.name] = w.isChecked() if f.is_bool else w.value()
                continue
            w = self._controls[f.name]
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
    Field("psa_l_lo", "PSA_l low", 0, 1024, tooltip="PSA_l low FFT bin index (~24.4 kHz per bin)."),
    Field("psa_l_hi", "PSA_l high", 0, 1024, tooltip="PSA_l high FFT bin index (~24.4 kHz per bin)."),
    Field("psa_w_lo", "PSA_w low", 0, 1024, tooltip="PSA_w low FFT bin index (~24.4 kHz per bin)."),
    Field("psa_w_hi", "PSA_w high", 0, 1024, tooltip="PSA_w high FFT bin index (~24.4 kHz per bin)."),
    # Watermark deliberately not exposed here -- same reasoning as PSD_FIELDS above.
]

VGA_FIELDS = [
    Field("fine_gain_milli", "Fine gain", 1, 60000, tooltip="Milli-units; 1500 = x1.50."),
    Field("coarse_gain_milli", "Coarse gain", 1, 60000, tooltip="Milli-units; 6000 = x6.00."),
    Field("fine_dac_code", "Fine DAC code", 0, 4095, optional=True, tooltip="Raw DAC code."),
]

SHAPER_FIELDS = [
    Field("peaking", "Peaking time", 0, 65535, tooltip="Peaking time (samples)."),
    Field("gap", "Gap time", 0, 65535, tooltip="Gap time (samples)."),
    Field("decay", "Decay", 0, 65535, tooltip="Decay / pole-zero time constant (samples)."),
    Field("enable", "Enable", is_bool=True),
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

        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def set_client(self, client: FciClient | None) -> None:
        for panel in self.panels:
            panel.set_client(client)

    def set_controls_enabled(self, enabled: bool) -> None:
        for panel in self.panels:
            panel.set_controls_enabled(enabled)
