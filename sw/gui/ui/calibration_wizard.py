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

Earlier versions of this wizard tried to actively FORCE a high capture rate before collecting --
first by parking the trigger at blr_core's live baseline (which turned out to be a completely
different numeric domain from what trigger_core's comparator actually uses, and never fired at
all), then by parking at threshold=0 directly (in the right domain, and confirmed to fire at a very
high rate). Both were abandoned. threshold=0 measurably hung the ENTIRE device -- not just trace
reads, every CLI command including a plain ping -- reproduced repeatedly and independent of two
separate firmware fixes for the specific races found along the way (both real bugs, both kept, see
bringup.c). The working theory: this hardware's capture-completion interrupt fires fast enough at
threshold=0 (bringup.c's own comment: "fires at kHz" for an in-band threshold) that the CPU can get
stuck perpetually servicing it and never return to the main loop that answers the CLI at all -- an
interrupt livelock, not a data race, and not something a firmware patch to this wizard's own call
path can fix. A sparse threshold at the identical depth/delay survived many consecutive calls
cleanly in the same testing.

So this wizard now does the opposite: it never touches the trigger threshold at all, only rising
(the user's chosen polarity) and depth/delay (widened for a bigger pre-trigger window per capture,
same as before). Whatever threshold is already configured when the wizard runs is what it collects
against -- which is exactly the premise the user's own request was built on: background radiation
always produces some pulses, so a sparse, already-reasonable threshold will accumulate enough
captures given enough patience, without ever forcing the pathological trigger rate that hangs the
device. If the current threshold genuinely is not triggering at all (e.g. left at 0 from a previous
failed calibration), this fails cleanly with a message saying so, rather than trying to fix that
itself by any means that risks the same hang.

Quality control still handles per-capture contamination: a real pulse landing inside a capture's
pre-trigger window inflates that ONE capture's own internal sigma well above the others', so each
captured window's sigma is compared against the average across all of them and any capture whose
own sigma exceeds that average is dropped before pooling.

Calls the config client (a RemoteFciClient -- see acquisition_worker.py and config_panel.py's own
docstring for why this is safe alongside the worker's concurrent polling) directly from the GUI
thread while its modal dialog is open -- appropriate here too since this is a short,
user-initiated, blocking action, not something in an automatic background path.
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
"""Fixed regardless of whatever the Trigger tab's own Depth is currently set to -- CALIBRATION_DELAY
needs depth >= 2*delay to actually deliver that much pre-trigger history."""
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

CAPTURE_TIMEOUT_S = 30.0
"""Longer than earlier versions of this wizard needed: collecting at whatever rate the current,
sparse threshold naturally produces (rather than forcing a high one) trades speed for never
touching the trigger rate that hung the device -- see module docstring."""
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
        """Never touches the trigger threshold (see module docstring for why forcing a high
        capture rate is now deliberately avoided) -- only rising and the widened delay/depth.
        Collects several fresh captures' pre-trigger windows at whatever rate the CURRENT
        threshold naturally produces, then drops any whose own sigma is above the group's average
        before pooling the rest. Returns (pooled_samples, n_survivors) -- never touches self, so
        callers own the restore/UI update around it."""
        self._client.set_trigger(rising=rising, delay=CALIBRATION_DELAY, depth=CALIBRATION_DEPTH)

        captures = self._poll_for_captures()
        if len(captures) < N_CAPTURES_MINIMUM:
            raise CalibrationError(
                f"Only {len(captures)} capture(s) observed in {CAPTURE_TIMEOUT_S:.0f}s at the "
                f"current trigger threshold ({original.threshold}) -- too few to calibrate from. "
                f"Set a threshold that is at least triggering occasionally first, and check the "
                f"detector/AFE chain is actually connected and producing signal."
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
