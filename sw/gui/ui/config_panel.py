"""Configuration tab: one group box per subsystem (trigger/BLR/PSD/FCI/VGA/shaper), each a thin
form over the matching fci_api get_*()/set_*() pair.

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
from typing import Any, Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fci_api import FciClient, FciError

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


class _SubsystemPanel(QGroupBox):
    def __init__(self, title: str, fields: list[Field], get_fn: Callable[[], Any],
                 set_fn: Callable[..., None]):
        super().__init__(title)
        self._fields = fields
        self._get_fn = get_fn
        self._set_fn = set_fn
        self._controls: dict[str, QWidget] = {}
        self._last: Any = None

        grid = QGridLayout(self)
        for row, f in enumerate(fields):
            grid.addWidget(QLabel(f.label + ":"), row, 0)
            if f.is_bool:
                w = QCheckBox()
            else:
                w = QSpinBox()
                w.setRange(f.minimum, f.maximum)
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

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_refresh.setEnabled(enabled)
        self.btn_apply.setEnabled(enabled)
        for f in self._fields:
            self._controls[f.name].setEnabled(enabled and not f.read_only)

    def refresh(self) -> None:
        try:
            cfg = self._get_fn()
        except FciError as e:
            # Non-blocking by design: refresh() runs automatically on every connect (via
            # ConfigPanel.set_client() -> refresh_all()), not only from this panel's own button, so
            # a transient failure here must never be able to stop and wait for a user click -- a
            # modal QMessageBox in this path previously froze the whole application on exactly that
            # sequence (a read failing during the automatic post-connect refresh).
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
                    w.setChecked(False) if f.is_bool else w.setValue(f.minimum)
                else:
                    # Does not exist in this bitstream; no value written here would ever apply.
                    w.setEnabled(False)
                continue
            w.setEnabled(not f.read_only)
            if f.is_bool:
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
            self._set_fn(**kwargs)
        except FciError as e:
            logger.warning(f"{self.title()}: apply failed: {e}")
            self.lbl_status.setText(f"Write failed: {e}")
        self.refresh()


TRIGGER_FIELDS = [
    Field("threshold", "Threshold (signed ADC code)", -32768, 32767),
    Field("rising", "Rising edge", is_bool=True),
    Field("delay", "Pre-trigger delay (samples)", 2, 256),
    Field("depth", "Capture depth (samples)", 1, 2048),
]

BLR_FIELDS = [
    Field("shift", "Shift k (tau = 2^k samples)", 0, 15),
    Field("gate_thr", "Gate threshold", 0, 16383),
    Field("holdoff", "Hold-off (samples)", 0, 4095),
    Field("bypass", "Bypass", is_bool=True),
    Field("hold", "Hold", is_bool=True),
    Field("baseline", "Baseline (live, read-only)", -32768, 32767, read_only=True),
    Field("gate_open", "Gate open (live, read-only)", is_bool=True, read_only=True),
]

PSD_FIELDS = [
    Field("pre_trigger", "Pre-trigger (must equal trigger delay)", 0, 65535),
    Field("pre_gate", "Pre-gate (samples)", 0, 65535),
    Field("short_gate", "Short gate (samples)", 0, 65535),
    Field("long_gate", "Long gate (samples)", 0, 65535),
    Field("baseline_ref", "Baseline reference (signed)", -32768, 32767),
    Field("watermark", "Watermark (0 disables)", 0, 32),
]

FCI_FIELDS = [
    Field("psa_l_lo", "PSA_l low bin", 0, 512),
    Field("psa_l_hi", "PSA_l high bin", 0, 512),
    Field("psa_w_lo", "PSA_w low bin", 0, 512),
    Field("psa_w_hi", "PSA_w high bin", 0, 512),
    Field("watermark", "Watermark (0 disables)", 0, 32, optional=True, settable_when_none=False),
]

VGA_FIELDS = [
    Field("fine_gain_milli", "Fine gain (milli-units, 1500 = x1.50)", 1, 60000),
    Field("coarse_gain_milli", "Coarse gain (milli-units, 6000 = x6.00)", 1, 60000),
    Field("fine_dac_code", "Fine DAC raw code", 0, 4095, optional=True),
]

SHAPER_FIELDS = [
    Field("peaking", "Peaking time (samples)", 0, 65535),
    Field("gap", "Gap time (samples)", 0, 65535),
    Field("decay", "Decay / pole-zero (samples)", 0, 65535),
    Field("enable", "Enable", is_bool=True),
]


class ConfigPanel(QWidget):
    """Constructed with no client (MainWindow builds all tabs upfront, disabled, before any
    connection exists -- matching the reference GUI's pattern). Call set_client() once a
    connection is established; every subsystem panel's get/set calls are bound through small
    lambdas that close over self._client, so they pick up whatever client is current at call
    time rather than needing the widgets rebuilt on reconnect.
    """

    def __init__(self):
        super().__init__()
        self._client: FciClient | None = None
        self.panels: list[_SubsystemPanel] = []

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)

        specs = [
            ("Trigger", TRIGGER_FIELDS, "get_trigger", "set_trigger"),
            ("Baseline Restorer", BLR_FIELDS, "get_blr", "set_blr"),
            ("PSD", PSD_FIELDS, "get_psd", "set_psd"),
            ("FCI", FCI_FIELDS, "get_fci", "set_fci"),
            ("VGA", VGA_FIELDS, "get_vga", "set_vga"),
            ("Pulse Shaper (may be absent from this bitstream)", SHAPER_FIELDS,
             "get_shaper", "set_shaper"),
        ]
        for title, fields, get_name, set_name in specs:
            panel = _SubsystemPanel(
                title,
                fields,
                get_fn=self._bind(get_name),
                set_fn=self._bind(set_name),
            )
            self.panels.append(panel)
            inner_layout.addWidget(panel)
        inner_layout.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.set_controls_enabled(False)

    def _bind(self, method_name: str) -> Callable[..., Any]:
        def call(*args, **kwargs):
            if self._client is None:
                raise RuntimeError(f"{method_name}() called with no client connected")
            return getattr(self._client, method_name)(*args, **kwargs)

        return call

    def set_client(self, client: FciClient | None) -> None:
        self._client = client
        self.set_controls_enabled(client is not None)
        if client is not None:
            self.refresh_all()

    def set_controls_enabled(self, enabled: bool) -> None:
        for panel in self.panels:
            panel.set_controls_enabled(enabled)

    def refresh_all(self) -> None:
        for panel in self.panels:
            panel.refresh()
