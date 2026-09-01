"""AcquisitionWorker: a QThread that owns one FciTransport/FciClient and is the ONLY thing that
calls into it during normal operation -- see fci_api.FciTransport's own docstring for why that
makes the RLock inside it a second line of defence rather than the only one.

Deliberately NOT using nested Qt event loops or QTimer inside this thread (a QThread subclass's own
slots are not automatically affinitized to the thread it runs on unless you use moveToThread(), a
well-known Qt/PySide gotcha). Instead this mirrors the reference project's own approach
(NSIL-Counter's SerialWorker): a plain Python loop in run(), and plain threading.Event flags for
the GUI thread to request things (start/stop acquisition, capture one trace) -- the worker's own
loop checks them once per iteration and acts on its own thread, so a trace capture can never race
a batch poll for the same transport.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QThread, Signal

from fci_api import AcqEvent, FciClient, FciError, FciTransport, TraceResult

RB_MAX_BATCH = 32
"""Events one $RB can return. Fixed by the hardware result FIFO's depth (result_fifo.vhd's
FIFO_DEPTH generic), and matched by cli.c's own RB_MAX_BATCH -- a full batch therefore means the
FIFO was saturated, not that the request was simply satisfied."""

logger = logging.getLogger(__name__)


class AcquisitionWorker(QThread):
    batch_received = Signal(list)  # list[AcqEvent]
    trace_received = Signal(object)  # TraceResult | None
    stats_received = Signal(object)  # fci_api.Stats
    acquisition_state_changed = Signal(bool)
    connection_changed = Signal(bool)
    error_occurred = Signal(str)

    def __init__(self, transport: FciTransport, poll_interval_s: float = 0.2,
                 stats_interval_s: float = 2.0, busy_interval_s: float = 0.0):
        super().__init__()
        self._transport = transport
        self._client = FciClient(transport)
        self._poll_interval_s = poll_interval_s
        self._busy_interval_s = busy_interval_s
        self._stats_interval_s = stats_interval_s

        self._stop_event = threading.Event()
        self._trace_request = threading.Event()
        self._trace_n = 2048
        self._scope_running = threading.Event()
        """Continuous ('Start') scope mode, distinct from _trace_request's one-shot ('Single').
        Checked every loop iteration while set, alongside the batch poll -- both are cheap enough
        (a $RT round trip is ~30 ms even for a full 2048-sample trace) to share one iteration
        without meaningfully slowing either down at the 200 ms default poll interval."""
        self._scope_n = 2048
        self._start_acq_request = threading.Event()
        self._stop_acq_request = threading.Event()
        self._batch_poll_suspended = threading.Event()
        """Set while the FoM sweep worker needs exclusive use of $RB for its own grid-point event
        collection. Without this, this worker's own unconditional per-iteration read_batch() call
        (below) would race the sweep's reads for the same 32-deep FIFO, splitting events between
        the two unpredictably and making "collect N fresh events for this grid point" unreliable."""

    # ---- thread-safe requests from the GUI thread; the worker's own loop acts on these ----

    def suspend_batch_polling(self) -> None:
        self._batch_poll_suspended.set()

    def resume_batch_polling(self) -> None:
        self._batch_poll_suspended.clear()

    def request_trace(self, n: int = 2048) -> None:
        """Requests one $RT capture on the worker's own thread ("Single"). Safe to call from any
        thread."""
        self._trace_n = n
        self._trace_request.set()

    def request_scope_start(self, n: int = 2048) -> None:
        """Starts continuously capturing and emitting traces ("Start"/"Run"), once per loop
        iteration, until request_scope_stop(). Safe to call from any thread."""
        self._scope_n = n
        self._scope_running.set()

    def request_scope_stop(self) -> None:
        self._scope_running.clear()

    def request_start_acquisition(self) -> None:
        self._start_acq_request.set()

    def request_stop_acquisition(self) -> None:
        self._stop_acq_request.set()

    def stop(self) -> None:
        """Signals the loop to exit and blocks until the thread has actually finished."""
        self._stop_event.set()
        self.wait()

    # ---- the thread body ----

    def run(self) -> None:
        try:
            self._transport.open()
        except Exception as e:  # pyserial raises plain OSError/SerialException, not FciError
            logger.error(f"failed to open {self._transport.port}: {e}")
            self.error_occurred.emit(f"Failed to open {self._transport.port}: {e}")
            self.connection_changed.emit(False)
            return

        logger.info(f"connected to {self._transport.port}")
        self.connection_changed.emit(True)

        stats_countdown = 0.0

        # try/finally, so the device is ALWAYS left disabled and the port always closed -- not
        # merely on the clean path. The shutdown used to sit after the loop, which meant any
        # exception escaping the body skipped it: the thread died, the GUI still looked connected,
        # and the DAQ was left running in $AE with nothing draining its FIFOs. That happened for
        # real when a malformed $RB reply raised out of the parser. Leaving hardware acquiring
        # after the client is gone is the worst of the possible outcomes here, so it is handled
        # structurally rather than by trusting the loop not to raise.
        try:
            self._run_loop(stats_countdown)
        finally:
            self._shutdown()

    def _run_loop(self, stats_countdown: float) -> None:
        while not self._stop_event.is_set():
            if self._start_acq_request.is_set():
                self._start_acq_request.clear()
                self._safe_call(self._client.enable_acquisition, "enable_acquisition")
                self.acquisition_state_changed.emit(True)

            if self._stop_acq_request.is_set():
                self._stop_acq_request.clear()
                self._safe_call(self._client.disable_acquisition, "disable_acquisition")
                self.acquisition_state_changed.emit(False)

            if self._trace_request.is_set():
                self._trace_request.clear()
                trace = self._safe_call(lambda: self._client.read_trace(self._trace_n), "read_trace")
                if trace is not _FAILED:
                    self.trace_received.emit(trace)
                # Deliberately no `continue` here: a trace capture and the batch poll below are
                # both cheap, and skipping this cycle's poll would just delay live data for no
                # benefit -- unlike request handling, there is no reason to prioritize one over
                # the other within a single loop iteration.

            if self._scope_running.is_set():
                trace = self._safe_call(lambda: self._client.read_trace(self._scope_n), "read_trace")
                if trace is not _FAILED:
                    self.trace_received.emit(trace)

            # Adaptive pacing: a FULL batch means the 32-deep hardware FIFO had more queued than
            # one request could carry, so events are actively being dropped -- poll again at once.
            # A short batch means we drained it, so fall back to the idle period.
            poll_wait = self._poll_interval_s
            if not self._batch_poll_suspended.is_set():
                events = self._safe_call(self._client.read_batch, "read_batch")
                if events is not _FAILED and events:
                    self.batch_received.emit(events)
                    if len(events) >= RB_MAX_BATCH:
                        poll_wait = self._busy_interval_s

            if stats_countdown <= 0:
                stats = self._safe_call(self._client.read_stats, "read_stats")
                if stats is not _FAILED:
                    self.stats_received.emit(stats)
                stats_countdown = self._stats_interval_s

            self._stop_event.wait(poll_wait)
            stats_countdown -= max(poll_wait, 0.001)

    def _shutdown(self) -> None:
        """Runs on every exit from the loop, clean or not. Each step is independently guarded:
        a failure disabling acquisition must not prevent the port being closed."""
        try:
            self._safe_call(self._client.disable_acquisition, "disable_acquisition (shutdown)")
        except Exception:  # noqa: BLE001 -- _safe_call should absorb this, belt and braces
            logger.exception("disable_acquisition raised during shutdown")
        try:
            self._transport.close()
            logger.info(f"disconnected from {self._transport.port}")
        except Exception:  # noqa: BLE001
            logger.exception("closing the transport raised during shutdown")
        self.connection_changed.emit(False)

    def _safe_call(self, fn, label: str):
        """Runs fn(), converting an FciError into an emitted signal instead of an uncaught
        exception that would silently kill this thread. Returns the sentinel _FAILED on error so
        callers can distinguish "got None" (a legitimate return value, e.g. no trace pending) from
        "the call raised"."""
        try:
            return fn()
        except FciError as e:
            logger.warning(f"{label} failed: {e}")
            self.error_occurred.emit(f"{label}: {e}")
            return _FAILED
        except Exception as e:  # noqa: BLE001 -- see below
            # Deliberately broad. This runs on the acquisition thread, and anything escaping here
            # kills it silently: the GUI keeps its "connected" state while no further events ever
            # arrive. That happened for real -- a malformed $RB reply raised ValueError out of the
            # parser, which is not an FciError, and took the thread down mid-run. Whatever the
            # cause, the right behaviour is to report it and keep polling, not to stop.
            logger.exception(f"{label} raised an unexpected error")
            self.error_occurred.emit(f"{label}: {type(e).__name__}: {e}")
            return _FAILED


class _FailedSentinel:
    def __repr__(self) -> str:
        return "<FAILED>"


_FAILED = _FailedSentinel()
