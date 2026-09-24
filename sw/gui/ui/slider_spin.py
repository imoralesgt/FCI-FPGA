"""Sliders bound to a spinbox, so a numeric config field can be set precisely (typing a value) or
quickly (dragging) -- both editing modes visible and usable at once, not a toggle between them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QSlider, QSpinBox, QWidget


class SliderSpinField(QWidget):
    """Exposes value()/setValue()/setEnabled(), matching QSpinBox's own interface, so it drops
    into SubsystemPanel's existing field-handling code (config_panel.py) with no other changes
    there. The slider and spinbox are wired bidirectionally to the same value; Qt only re-emits
    valueChanged when a value actually changes, so this settles in one step rather than looping.
    """

    valueChanged = Signal(int)

    def __init__(self, minimum: int = 0, maximum: int = 100, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.spin = QSpinBox()
        self.spin.setMaximumWidth(70)  # keeps the slider as the dominant control, not the spinbox
        self.setRange(minimum, maximum)

        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.spin.valueChanged.connect(self.valueChanged.emit)

        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.spin)

    def setRange(self, minimum: int, maximum: int) -> None:
        self.slider.setRange(minimum, maximum)
        self.spin.setRange(minimum, maximum)

    def value(self) -> int:
        return self.spin.value()

    def setValue(self, value: int) -> None:
        self.spin.setValue(value)

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.slider.setEnabled(enabled)
        self.spin.setEnabled(enabled)


class CycleTimeField(QWidget):
    """Like SliderSpinField, but the numeric side displays microseconds instead of a raw cycle
    count, stepping by exactly one clock period so every value it can land on is an exact integer
    cycle count -- the microsecond box IS the way to set "N cycles", not a unit shown alongside a
    separate cycle control. value()/setValue() still speak whole cycles, matching
    SliderSpinField's own interface exactly, so this drops into SubsystemPanel's existing
    field-handling code (config_panel.py) for a field whose WIRE unit is a clock cycle count but
    whose natural human unit is time -- pulse_shaper_core's peaking/flat_top/decay, so far the only
    fields in this GUI where that gap exists (see config_panel.py's Field.cycle_period_ns).
    """

    valueChanged = Signal(int)

    def __init__(self, minimum: int, maximum: int, cycle_period_ns: float, parent=None):
        super().__init__(parent)
        self._period_us = cycle_period_ns / 1000.0
        self._syncing = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minimum, maximum)

        self.spin = QDoubleSpinBox()
        # 3 decimals is enough to show a 20 ns (50 MHz) step -- 0.020 us -- as a distinct value
        # rather than rounding two adjacent cycles to the same displayed number.
        self.spin.setDecimals(3)
        self.spin.setSingleStep(self._period_us)
        self.spin.setRange(minimum * self._period_us, maximum * self._period_us)
        self.spin.setSuffix(" µs")
        self.spin.setMaximumWidth(90)

        self.slider.valueChanged.connect(self._on_slider_changed)
        self.spin.valueChanged.connect(self._on_spin_changed)

        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.spin)

    def _on_slider_changed(self, cycles: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.spin.setValue(cycles * self._period_us)
        self._syncing = False
        self.valueChanged.emit(cycles)

    def _on_spin_changed(self, us: float) -> None:
        if self._syncing:
            return
        # Typing (or arrow-stepping) a time snaps it to the nearest whole cycle -- the spin box's
        # OWN displayed value is re-set to that snapped time, not just the slider, so a typed
        # "1.234" us settles visibly to whatever exact cycle count it rounded to rather than
        # silently disagreeing with the value actually sent to the device.
        cycles = int(round(us / self._period_us))
        self._syncing = True
        self.spin.setValue(cycles * self._period_us)
        self.slider.setValue(cycles)
        self._syncing = False
        self.valueChanged.emit(cycles)

    def setRange(self, minimum: int, maximum: int) -> None:
        self.slider.setRange(minimum, maximum)
        self.spin.setRange(minimum * self._period_us, maximum * self._period_us)

    def value(self) -> int:
        return self.slider.value()

    def setValue(self, value: int) -> None:
        # Both widgets are set explicitly, not left to valueChanged propagation: Qt does not emit
        # a signal when setValue() doesn't change a widget's own current value, which would leave
        # the OTHER widget stale if only one were set and it happened to already match.
        self._syncing = True
        self.slider.setValue(value)
        self.spin.setValue(value * self._period_us)
        self._syncing = False

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.slider.setEnabled(enabled)
        self.spin.setEnabled(enabled)
