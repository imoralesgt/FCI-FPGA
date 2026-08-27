"""Automatic baseline/threshold calibration wizard, matching how firmware's own
calibrate_threshold() does it (bringup.c) rather than treating a whole captured trace as noise.

Firmware's approach, precisely: background radiation always produces some pulses, so instead of
waiting for a deliberately quiet moment, it measures the baseline from the PRE-TRIGGER portion of
real triggered captures -- the samples recorded before whatever caused the trigger, which are quiet
regardless of what the trigger itself fired on (bringup.c's own comment: "true even when the
trigger itself fired on noise, which is exactly the situation this recovers from"). It pools this
across SEVERAL captures for good statistics, then sets threshold = mean + N*sigma.

This wizard does the same thing over the CLI rather than raw MMIO, with one improvement: firmware's
own BASELINE_SAMPLES is only 64 (limited by TRIGGER_DELAY=100 at cold boot, before anything else is
configurable), but this wizard temporarily widens the pre-trigger window to the hardware's actual
maximum (delay=256, requiring depth>=512 -- see CALIBRATION_DEPTH/DELAY) for a much bigger
per-capture sample, pooled across multiple captures for better statistics with fewer of them, per
the user's own request. depth/delay are ALWAYS restored to whatever they were before running,
whether calibration succeeds, fails, or is cancelled -- only the proposed threshold/rising are a
lasting change, and only once the user explicitly applies them.

Like bringup.c, this ALWAYS parks the trigger at the noise band before collecting -- not only when
nothing is triggering at all. Measured directly on this hardware (2026-08-27): leaving whatever
operational threshold was already configured in place while collecting produced grossly inflated
statistics (sigma ~1600 against a baseline noise floor measured elsewhere this session at 7-170),
because that threshold's own "pre-trigger" window was capturing the rise of the real pulse it was
tuned to catch, not quiet baseline.

Finding the park point: an earlier version of this wizard used blr_core's own live baseline
estimate directly as the park threshold. Measured directly on this hardware: that reads in a
completely different numeric domain from what trigger_core's comparator (and read_trace()) actually
use -- blr_core reported a baseline around -6380 while raw trace samples cluster near 0, putting
the threshold thousands of counts below every real sample and latching the rising-edge comparator
permanently true (confirmed over a full 60s observation window: exactly one capture, ever -- the
same "threshold well below baseline never fires" failure mode bringup.c's own comments describe).

The fix: park at threshold=0 directly, in the SAME domain read_trace() itself reports, rather than
deriving a park point from any other register. This is correct specifically because BLR keeps the
restored baseline close to zero already (blr_core's whole job) -- sweeping across the ADC's full
span, the way bringup.c's find_noise_band() does at cold boot before BLR has necessarily settled,
is not needed here. Quality control handles the rest: a real pulse landing inside a capture's
pre-trigger window inflates that ONE capture's own internal sigma well above the others', so each
captured window's sigma is compared against the average across all of them and any capture whose
own sigma exceeds that average is dropped before pooling -- cheaper and more direct than hunting
for a "cleaner" park threshold, since it works on whatever park threshold is used.

Calls FciClient directly from the GUI thread while its modal dialog is open, the same deliberate
exception config_panel.py documents (FciTransport's RLock is what makes this safe alongside the
worker thread's concurrent polling) -- appropriate here too since this is a short, user-initiated,
blocking action, not something in an automatic background path.
"""

from __future__ import annotations

import logging
import statistics
import time

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from fci_api import FciClient, FciError

logger = logging.getLogger(__name__)

CALIBRATION_DEPTH = 512
"""Fixed regardless of whatever the oscilloscope's own Depth is currently set to -- both because
CALIBRATION_DELAY needs depth >= 2*delay to actually deliver that much pre-trigger history, and
because 512 keeps each capture well clear of the known FSL-read hang risk (see TRIGGER_FIELDS'
Depth tooltip in config_panel.py) even though this polls read_trace() repeatedly."""
CALIBRATION_DELAY = CALIBRATION_DEPTH // 2
"""256 -- the hardware's own delay ceiling (trigger_core: "valid range 2..256"). This is "half the
trace length" per the user's request, and also happens to be the largest pre-trigger window the
hardware can produce at all, which is the point: more quiet samples per capture, so fewer captures
are needed for good statistics."""

N_CAPTURES_RAW_TARGET = 20
"""Captures collected BEFORE quality filtering. Roughly half are expected to survive (an
above-average-sigma capture is discarded -- see module docstring), so this is double
N_CAPTURES_MINIMUM's old single-stage target to land on a similar number of surviving, pooled
samples in the end."""
N_CAPTURES_MINIMUM = 3
"""Proceed with whatever survives filtering once the timeout hits, as long as at least this many
captures remain -- background rate varies, and demanding the full target would make calibration
fail on a slow run for no good reason."""

CAPTURE_TIMEOUT_S = 20.0
CAPTURE_POLL_INTERVAL_S = 0.15
"""How often read_trace() is polled while collecting -- background pulses observed this session run
at a few Hz to a few tens of Hz, so this comfortably catches fresh captures without hammering the
link."""


class CalibrationError(Exception):
    pass


class CalibrationWizard(QDialog):
    def __init__(self, client: FciClient, parent=None):
        super().__init__(parent)
        self._client = client
        self._proposed_threshold: int | None = None
        self._proposed_rising: bool | None = None

        self.setWindowTitle("Baseline / Threshold Calibration")
        self.setModal(True)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.spin_sigma = QDoubleSpinBox()
        self.spin_sigma.setRange(1.0, 20.0)
        self.spin_sigma.setSingleStep(0.5)
        self.spin_sigma.setValue(8.0)  # matches THRESHOLD_SIGMA_MULT in bringup.c
        self.spin_sigma.setToolTip("Threshold is set this many standard deviations away from the "
                                    "measured baseline mean.")
        form.addRow("Standard deviations from baseline:", self.spin_sigma)

        self.combo_polarity = QComboBox()
        self.combo_polarity.addItem("Positive", True)
        self.combo_polarity.addItem("Negative", False)
        form.addRow("Pulse polarity:", self.combo_polarity)
        layout.addLayout(form)

        self.btn_run = QPushButton("Capture && Compute")
        self.btn_run.clicked.connect(self._run_calibration)
        layout.addWidget(self.btn_run)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate/busy style -- collection is a few seconds
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        layout.addWidget(self.lbl_result)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _run_calibration(self) -> None:
        self.btn_run.setEnabled(False)
        self.lbl_result.setText("Collecting captures...")
        self.progress_bar.setVisible(True)
        try:
            original = self._client.get_trigger()
        except FciError as e:
            QMessageBox.warning(self, "Calibration Failed", f"Could not read trigger config: {e}")
            self.btn_run.setEnabled(True)
            self.progress_bar.setVisible(False)
            return

        rising = bool(self.combo_polarity.currentData())
        try:
            baseline_samples, n_survivors = self._collect_pooled_baseline(original, rising)
        except CalibrationError as e:
            QMessageBox.warning(self, "Calibration Failed", str(e))
            self.lbl_result.setText("")
            self.btn_run.setEnabled(True)
            self.progress_bar.setVisible(False)
            return
        except FciError as e:
            QMessageBox.warning(self, "Calibration Failed", f"Device communication error: {e}")
            self.lbl_result.setText("")
            self.btn_run.setEnabled(True)
            self.progress_bar.setVisible(False)
            return
        finally:
            # depth/delay (and any bootstrap threshold) were only ever temporary instrumentation --
            # always put them back, regardless of how collection went.
            try:
                self._client.set_trigger(threshold=original.threshold, rising=original.rising,
                                          delay=original.delay, depth=original.depth)
            except FciError as e:
                logger.warning(f"could not restore original trigger config after calibration: {e}")
            self.btn_run.setEnabled(True)
            self.progress_bar.setVisible(False)

        mean = statistics.fmean(baseline_samples)
        sigma = statistics.pstdev(baseline_samples, mu=mean)
        n_sigma = self.spin_sigma.value()
        raw_threshold = mean + n_sigma * sigma if rising else mean - n_sigma * sigma
        threshold = max(-32768, min(32767, round(raw_threshold)))

        self._proposed_threshold = threshold
        self._proposed_rising = rising
        self.lbl_result.setText(
            f"Captures kept: {n_survivors}<br>"
            f"Pre-trigger samples pooled: {len(baseline_samples)}<br>"
            f"Baseline mean: {mean:.1f}<br>"
            f"Sigma: {sigma:.1f}<br>"
            f"Proposed threshold: "
            f"<b><span style='color:#e63030;'>"
            f"{threshold} ({'rising' if rising else 'falling'} edge)</span></b>"
        )
        self.adjustSize()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def _collect_pooled_baseline(self, original, rising: bool) -> tuple[list[int], int]:
        """Parks the trigger at threshold=0 (see module docstring for why this always happens, not
        only when nothing is triggering, and why zero rather than a swept or probed value), widens
        delay/depth, collects several fresh captures' pre-trigger windows, then drops any whose own
        sigma is above the group's average before pooling the rest. Returns (pooled_samples,
        n_survivors) -- never touches self, so callers own the restore/UI update around it."""
        self._client.set_trigger(threshold=0, rising=rising,
                                  delay=CALIBRATION_DELAY, depth=CALIBRATION_DEPTH)

        captures = self._poll_for_captures()
        if len(captures) < N_CAPTURES_MINIMUM:
            raise CalibrationError(
                f"Only {len(captures)} capture(s) observed in {CAPTURE_TIMEOUT_S:.0f}s while "
                f"parked at threshold=0 -- too few to calibrate from. Check the detector/AFE "
                f"chain is actually connected and producing signal."
            )

        per_capture_sigma = [statistics.pstdev(c) for c in captures]
        avg_sigma = statistics.fmean(per_capture_sigma)
        survivors = [c for c, s in zip(captures, per_capture_sigma) if s <= avg_sigma]
        logger.info(f"calibration: {len(captures)} captures, average sigma {avg_sigma:.1f}, "
                    f"{len(survivors)} kept after dropping above-average ones")
        if len(survivors) < N_CAPTURES_MINIMUM:
            raise CalibrationError(
                f"Only {len(survivors)} of {len(captures)} captures survived quality filtering -- "
                f"too few to calibrate from. The background may be unusually active right now."
            )

        pooled = [s for c in survivors for s in c]
        return pooled, len(survivors)

    def _poll_for_captures(self) -> list[list[int]]:
        """Returns one entry per fresh capture, each its own list of CALIBRATION_DELAY pre-trigger
        samples -- kept separate (not flattened) so the caller can score and filter each capture
        individually before pooling."""
        captures: list[list[int]] = []
        last_trace: list[int] | None = None
        deadline = time.monotonic() + CAPTURE_TIMEOUT_S
        while len(captures) < N_CAPTURES_RAW_TARGET and time.monotonic() < deadline:
            trace = self._client.read_trace(CALIBRATION_DEPTH)
            if (len(trace.samples) >= CALIBRATION_DELAY
                    and trace.samples != last_trace):
                last_trace = trace.samples
                captures.append(trace.samples[:CALIBRATION_DELAY])
            self.lbl_result.setText(f"Collecting captures... ({len(captures)}/{N_CAPTURES_RAW_TARGET})")
            # This whole method runs on the GUI thread (see module docstring for why), so without
            # this the progress bar would never actually animate and the window would look frozen
            # for the few seconds this loop runs.
            QApplication.processEvents()
            time.sleep(CAPTURE_POLL_INTERVAL_S)
        return captures

    def apply_to_device(self) -> None:
        """Only meaningful after accept() -- writes the proposed threshold/rising via $ST. Left for
        the caller to invoke explicitly (rather than doing it in accept() itself) so a write
        failure can be reported without the dialog having already closed. Delay/depth are NOT
        touched here -- they were already restored to their pre-calibration values in
        _run_calibration()'s finally block."""
        if self._proposed_threshold is None:
            return
        self._client.set_trigger(threshold=self._proposed_threshold, rising=self._proposed_rising)
        logger.info(f"Calibration applied: threshold={self._proposed_threshold} "
                    f"rising={self._proposed_rising}")
