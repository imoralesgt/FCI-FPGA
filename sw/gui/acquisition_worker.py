"""AcquisitionWorker: supervises the device-I/O child process (fci_api.reader_process) and
translates its evt_q messages into the same Qt signals this class has always emitted.

This class no longer touches the serial port itself -- see fci_api/reader_process.py's module
docstring for why (project log section 8i/8k: a GUI redraw holding the GIL for ~40+ ms was enough
to overrun the kernel's UART receive buffer and corrupt `$RQ` frames; the only structural fix is
giving the device I/O its own process, hence its own GIL, so nothing the GUI does can ever delay a
read). What used to be a plain Python loop in QThread.run() reading the port directly is now a
`multiprocessing.Process` running that loop in reader_process.py; this class's own QThread.run()
does much less work -- it drains the child's evt_q and re-emits Qt signals, exactly as before, so
every call site in controllers.py is unchanged.

Config panels, the calibration wizard, and the FoM sweep worker used to hold a plain fci_api.
FciClient and call it directly from their own thread, relying on FciTransport's RLock to serialize
against this worker's concurrent polling (see config_panel.py's docstring for that reasoning, now
historical). The transport lives only in the child process, so there is no longer a shared
FciTransport for an RLock to protect -- instead, make_client() returns a RemoteFciClient, which
proxies every fci_api.FciClient method across the process boundary as an RPC and blocks the caller
until the child replies. It matches FciClient's public interface closely enough (duck typing, not
inheritance) that no other file needed to change.
"""

from __future__ import annotations

import itertools
import logging
import multiprocessing
import queue
import threading

from PySide6.QtCore import QThread, Signal

import config
from fci_api.exceptions import (
    FciError,
    FciNotPresentError,
    FciParamError,
    FciProtocolError,
    FciTimeoutError,
    FciUnknownCommandError,
)
from fci_api.reader_process import run_reader_process

logger = logging.getLogger(__name__)

RB_MAX_BATCH = 1024
"""Events one batch request may return, matching cli.c's RB_MAX_BATCH and the result FIFO depth.

Sized for the FTDI latency timer at its 16 ms DEFAULT, because the instrument has to work on any
host without root or a udev rule. The timer only delays the final partial USB packet, so a large
reply pays it once and batch size amortises it: with the binary $RQ encoding, batch 32 gives
~1300 ev/s and batch 1024 gives ~3486 ev/s against a 3686 ev/s link ceiling. Tuning the timer to
1 ms then adds almost nothing, which is the point -- it must not be a deployment requirement.

Asking for the maximum is free when little is pending: the device stops early once the FIFO
empties. The long transaction only happens when the FIFO is genuinely full, which is when
draining it fast matters more than command latency. (Also defined in fci_api.reader_process,
which is the process that actually acts on it -- this copy is for the docstring above and for
any caller here that wants the same number, e.g. log messages.)"""

_READER_LOG_PATH = str(config.LOG_DIR / "fci_gui_reader.log")
"""Separate from fci_gui.log on purpose -- see reader_process._configure_logging()'s docstring:
two independent RotatingFileHandlers, one per process, must never share a file."""

_ERROR_TYPES: dict[str, type[FciError]] = {
    "FciError": FciError,
    "FciProtocolError": FciProtocolError,
    "FciUnknownCommandError": FciUnknownCommandError,
    "FciParamError": FciParamError,
    "FciTimeoutError": FciTimeoutError,
    "FciNotPresentError": FciNotPresentError,
}


def _reraise_from_child(error_type: str, message: str) -> None:
    """Reconstructs and raises the exception an RPC failed with in the CHILD process, as the
    SAME exception subtype, so callers doing `except FciParamError` or `except FciError` keep
    working across the process boundary exactly as they did against a local FciClient.

    Deliberately does not call the subtype's own __init__ (FciCommandError's takes `request`, not
    a message -- signatures differ per subtype and are not worth replicating here). Instead builds
    a bare instance via __new__ and sets its message directly through the base Exception.__init__,
    which is enough for isinstance() checks and str(e) to both behave correctly; only the object's
    OWN extra attributes (like FciCommandError.request) are not reconstructed, and nothing in this
    codebase reads those from a caught exception.

    An error_type not in _ERROR_TYPES means the child hit something that was never an FciError to
    begin with (a genuine bug, not a device-reported condition) -- raised as RuntimeError so it is
    visible rather than silently mapped onto FciError, which callers filter on and would swallow."""
    cls = _ERROR_TYPES.get(error_type)
    if cls is None:
        raise RuntimeError(f"reader process: {error_type}: {message}")
    exc = cls.__new__(cls)
    Exception.__init__(exc, message)
    raise exc


class RemoteFciClient:
    """Drop-in for fci_api.FciClient (duck-typed, not a subclass) whose every method is an RPC to
    the reader process instead of a direct transport call -- see this module's own docstring.

    No method list of its own: __getattr__ proxies ANY non-underscore attribute access as a
    same-named RPC, so a new FciClient method works here with no change, and a typo still fails
    at call time (the child's own getattr(client, name) raises AttributeError, forwarded back
    through _reraise_from_child like any other exception) rather than earlier or later than it
    would against a real client.

    Every call blocks the calling thread until the child replies or _RPC_TIMEOUT_S elapses --
    the same blocking behaviour a direct client call always had (it blocked on the serial round
    trip), just crossing a queue instead of a wire now. SubsystemPanel, the calibration wizard,
    and the FoM sweep worker all call synchronously and expect exactly this.
    """

    _RPC_TIMEOUT_S = 5.0
    """Generous relative to any single RPC: the reader process services cmd_q at the top of every
    loop iteration, ahead of its own batch/scope/stats work, so real latency is a small fraction
    of this. This bound exists to fail loudly if the child has died or deadlocked, not to be a
    tuned budget."""

    def __init__(self, issue_rpc):
        self._issue_rpc = issue_rpc

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            return self._issue_rpc(name, args, kwargs)

        return _call


class AcquisitionWorker(QThread):
    batch_received = Signal(list)  # list[AcqEvent]
    amplitude_batch_received = Signal(list)  # list[AmpEvent] -- see reader_process.py's $RA mode
    trace_received = Signal(object)  # TraceResult | None
    stats_received = Signal(object)  # fci_api.Stats
    acquisition_state_changed = Signal(bool)
    connection_changed = Signal(bool)
    error_occurred = Signal(str)

    def __init__(self, port: str, poll_interval_s: float = 0.2,
                 stats_interval_s: float = 2.0, busy_interval_s: float = 0.0,
                 scope_interval_s: float = 0.5):
        super().__init__()
        self._port = port
        self._poll_interval_s = poll_interval_s
        self._stats_interval_s = stats_interval_s
        self._busy_interval_s = busy_interval_s
        self._scope_interval_s = scope_interval_s

        # spawn, not fork: this process holds Qt state (event loop, widgets, other threads), none
        # of which should be duplicated into the child. spawn re-imports cleanly instead, at the
        # cost of the target/args needing to be picklable -- true here (a module-level function,
        # plain str/float args, and Queue objects made via this same context).
        self._mp_ctx = multiprocessing.get_context("spawn")
        self._cmd_q = self._mp_ctx.Queue()
        self._evt_q = self._mp_ctx.Queue()
        self._proc: multiprocessing.process.BaseProcess | None = None

        self._stop_event = threading.Event()
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._pending_lock = threading.Lock()
        self._next_id = itertools.count(1)

    # ---- device access for everything that isn't the streaming poll loop ----

    def make_client(self) -> RemoteFciClient:
        """One RemoteFciClient per connection, matching how controllers.py used to build one
        FciClient per connection. Cheap to call more than once if ever needed -- it holds no
        per-instance state beyond the shared RPC plumbing below."""
        return RemoteFciClient(self._issue_rpc)

    def _issue_rpc(self, method: str, args: tuple, kwargs: dict):
        """Called from ANY thread (the GUI thread via a config panel, or FomSweepWorker's own
        thread) -- registers a pending call keyed by a fresh id, posts it to cmd_q, and blocks on
        a private threading.Event until run()'s evt_q-draining loop sees the matching reply and
        sets it. This is a minimal hand-rolled Future; concurrent.futures wasn't reached for
        because both ends of the wiring (post to cmd_q, wait on an Event) are two lines each and
        the alternative would still need this same pending-calls dict to route replies by id."""
        call_id = next(self._next_id)
        done = threading.Event()
        box: dict = {}
        with self._pending_lock:
            self._pending[call_id] = (done, box)
        self._cmd_q.put({"type": "rpc", "id": call_id, "method": method,
                          "args": args, "kwargs": kwargs})
        try:
            if not done.wait(RemoteFciClient._RPC_TIMEOUT_S):
                raise FciTimeoutError(
                    f"{method}: no reply from reader process within "
                    f"{RemoteFciClient._RPC_TIMEOUT_S}s (process alive: "
                    f"{self._proc.is_alive() if self._proc else False})"
                )
        finally:
            with self._pending_lock:
                self._pending.pop(call_id, None)
        if box.get("error"):
            _reraise_from_child(box["error_type"], box["message"])
        return box.get("value")

    # ---- thread-safe requests from the GUI thread; the child's own loop acts on these ----

    def suspend_batch_polling(self) -> None:
        self._cmd_q.put({"type": "suspend_batch_polling"})

    def resume_batch_polling(self) -> None:
        self._cmd_q.put({"type": "resume_batch_polling"})

    def request_trace(self, n: int = 2048) -> None:
        """Requests one $RT capture on the reader process ("Single"). Safe to call from any
        thread."""
        self._cmd_q.put({"type": "trace_once", "n": n})

    def request_scope_start(self, n: int = 2048) -> None:
        """Starts continuously capturing and emitting traces ("Start"/"Run"), until
        request_scope_stop(). Safe to call from any thread."""
        self._cmd_q.put({"type": "scope_start", "n": n})

    def request_scope_stop(self) -> None:
        self._cmd_q.put({"type": "scope_stop"})

    def request_start_acquisition(self) -> None:
        self._cmd_q.put({"type": "start_acq"})

    def request_stop_acquisition(self) -> None:
        self._cmd_q.put({"type": "stop_acq"})

    def request_spectrum_poll(self, enabled: bool) -> None:
        """Tells the reader process whether the Spectrum tab wants amplitude-only data via $RA.
        Only takes effect while full acquisition (request_start_acquisition/stop) is NOT running --
        see reader_process.py's mode-selection comment for why the two are mutually exclusive
        rather than both polled."""
        self._cmd_q.put({"type": "spectrum_poll", "enabled": enabled})

    def stop(self) -> None:
        """Signals the reader process to shut down and this thread's evt_q pump to exit, then
        blocks until both have actually finished."""
        self._cmd_q.put({"type": "shutdown"})
        self._stop_event.set()
        self.wait()
        if self._proc is not None:
            self._proc.join(timeout=2.0)
            if self._proc.is_alive():
                logger.warning("reader process did not exit cleanly; terminating")
                self._proc.terminate()
                self._proc.join(timeout=1.0)

    # ---- the thread body: spawn the child, then pump evt_q into Qt signals ----

    def run(self) -> None:
        self._proc = self._mp_ctx.Process(
            target=run_reader_process,
            args=(self._port, self._poll_interval_s, self._stats_interval_s,
                  self._busy_interval_s, self._scope_interval_s, self._cmd_q, self._evt_q,
                  _READER_LOG_PATH),
            daemon=True,
        )
        self._proc.start()

        while not self._stop_event.is_set():
            try:
                msg = self._evt_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._dispatch(msg)

        # Drain briefly for the "disconnected" message the child's shutdown path sends, so
        # on_connection_changed(False) still fires even when stop() raced the child's own exit.
        try:
            while True:
                self._dispatch(self._evt_q.get(timeout=0.5))
        except queue.Empty:
            pass

    def _dispatch(self, msg: dict) -> None:
        t = msg["type"]
        if t == "connected":
            self.connection_changed.emit(True)
        elif t == "connect_failed":
            logger.error(f"failed to open {self._port}: {msg['error']}")
            self.error_occurred.emit(f"Failed to open {self._port}: {msg['error']}")
            self.connection_changed.emit(False)
        elif t == "disconnected":
            self.connection_changed.emit(False)
        elif t == "batch":
            self.batch_received.emit(msg["events"])
        elif t == "amplitude_batch":
            self.amplitude_batch_received.emit(msg["events"])
        elif t == "trace":
            self.trace_received.emit(msg["trace"])
        elif t == "stats":
            self.stats_received.emit(msg["stats"])
        elif t == "acq_state":
            self.acquisition_state_changed.emit(msg["running"])
        elif t == "error":
            self.error_occurred.emit(msg["message"])
        elif t in ("rpc_result", "rpc_error"):
            with self._pending_lock:
                entry = self._pending.get(msg["id"])
            if entry is None:
                return  # the caller already timed out and stopped waiting; nothing to deliver to
            done, box = entry
            if t == "rpc_result":
                box["value"] = msg["value"]
            else:
                box["error"] = True
                box["error_type"] = msg["error_type"]
                box["message"] = msg["message"]
            done.set()
        else:
            logger.warning(f"acquisition worker: unknown message type {t!r}")
