"""A slider bound to a spinbox, so a numeric config field can be set precisely (typing a value) or
quickly (dragging) -- both editing modes visible and usable at once, not a toggle between them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QSlider, QSpinBox, QWidget


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
