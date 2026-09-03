"""The device-I/O side of the reader-process split (project log section 8i/8k).

Why this exists: every byte to/from the board used to move on a QThread inside the GUI process.
That thread and the GUI's own redraws share one GIL, and a GUI redraw is Python code holding it
for tens of milliseconds at a time. Section 8i measured the consequence directly with
TIOCGICOUNT: a ~20 ms stall was harmless, but at ~40 ms the kernel's UART receive buffer started
overrunning -- bytes silently dropped, `$RQ` frame alignment lost -- and delivered throughput fell
double digits of percent. No amount of buffering fixes a stall longer than the buffer, so the fix
is structural: run this loop in its own OS process, with its own GIL, so nothing the GUI does can
ever delay a read from the serial port.

This module is the entire child side of that split: it owns the ONE `FciTransport`/`FciClient` for
the life of the connection and is the only thing that ever touches the serial port. It knows
nothing about Qt. Everything it needs to do arrives as a dict on `cmd_q`; everything it has to
report leaves as a dict on `evt_q`. `gui/acquisition_worker.py` is the parent-side counterpart:
it spawns this as a `multiprocessing.Process`, translates `evt_q` into Qt signals, and translates
Qt/GUI-thread calls into `cmd_q` messages.

Message shapes (plain dicts -- multiprocessing.Queue pickles them, and every payload here is
already a plain dataclass or builtin, so no custom serialisation is needed):

  cmd_q (parent -> child):
    {"type": "rpc", "id": int, "method": str, "args": tuple, "kwargs": dict}
        Any fci_api.FciClient method, called by name. This is how config panels, the calibration
        wizard, and the FoM sweep worker reach the device now -- see acquisition_worker.py's
        RemoteFciClient, which is what actually sends these.
    {"type": "start_acq"} / {"type": "stop_acq"}
    {"type": "scope_start", "n": int} / {"type": "scope_stop"}
    {"type": "trace_once", "n": int}
    {"type": "suspend_batch_polling"} / {"type": "resume_batch_polling"}
    {"type": "shutdown"}

  evt_q (child -> parent):
    {"type": "connected"} / {"type": "connect_failed", "error": str} / {"type": "disconnected"}
    {"type": "batch", "events": list[AcqEvent]}
    {"type": "trace", "trace": TraceResult | None}
    {"type": "stats", "stats": Stats}
    {"type": "acq_state", "running": bool}
    {"type": "error", "message": str}
    {"type": "rpc_result", "id": int, "value": object}
    {"type": "rpc_error", "id": int, "error_type": str, "message": str}

RPCs are serviced FIRST in every loop iteration, ahead of the batch/scope/stats work below --
this is what keeps a config panel's Refresh/Apply feeling instant despite the round trip now
crossing a process boundary rather than just acquiring an in-process RLock. The wait between
iterations is a blocking `cmd_q.get(timeout=poll_wait)`, not a fixed sleep, precisely so a
just-arrived RPC does not have to wait out the rest of an idle poll period.
"""

from __future__ import annotations

import logging
import queue
import time

from .client import FciClient
from .exceptions import FciError, FciUnknownCommandError
from .transport import FciTransport

logger = logging.getLogger(__name__)

RB_MAX_BATCH = 1024
"""See acquisition_worker.py's copy of this constant for the full sizing rationale (FTDI latency
timer at its unconfigurable 16 ms default). Duplicated rather than imported because this module
must not import anything from gui/ -- it runs in a separate process with no Qt available, and
gui/acquisition_worker.py already documents the value; keeping both is one small constant, not a
second copy of a hardware fact the way the project's own style guidance warns against elsewhere."""


class _Failed:
    def __repr__(self) -> str:
        return "<FAILED>"


_FAILED = _Failed()


def _configure_logging(log_path: str) -> None:
    """Minimal stdlib-only logging setup for this process, writing to its OWN file -- deliberately
    not gui/logger_config.py's, and not the GUI's fci_gui.log: with "spawn" as the start method,
    the GUI process's own logging setup runs exactly once (gui/main.py's __main__ guard), so this
    process has no handlers at all unless it configures its own. Sharing fci_gui.log instead would
    mean two independent RotatingFileHandlers, in two OS processes, racing to rotate the same file
    with no cross-process coordination. This module stays free of any gui/ import (its own module
    docstring), so this is intentionally NOT gui/config.py's LOG_DIR -- the caller resolves that
    and passes the path in."""
    import logging.handlers
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] (%(processName)s) %(name)s:%(lineno)d - %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def run_reader_process(port: str, poll_interval_s: float, stats_interval_s: float,
                        busy_interval_s: float, scope_interval_s: float,
                        cmd_q, evt_q, log_path: str | None = None) -> None:
    """Entry point for the child process (`multiprocessing.Process(target=run_reader_process, ...)`
    in acquisition_worker.py). Blocks until a "shutdown" command arrives or the port fails to open.
    `log_path`, if given, is where this process's OWN log file goes -- see _configure_logging().
    """
    if log_path is not None:
        _configure_logging(log_path)

    transport = FciTransport(port)
    client = FciClient(transport)

    try:
        transport.open()
    except Exception as e:  # pyserial raises plain OSError/SerialException, not FciError
        logger.error(f"failed to open {port}: {e}")
        evt_q.put({"type": "connect_failed", "error": str(e)})
        return

    logger.info(f"connected to {port}")
    evt_q.put({"type": "connected"})

    try:
        _run_loop(client, poll_interval_s, stats_interval_s, busy_interval_s, scope_interval_s,
                  cmd_q, evt_q)
    finally:
        # Same reasoning as the QThread version's try/finally: whatever ends the loop, the device
        # must be left disabled and the port closed, not just on the tidy shutdown path.
        try:
            _safe_call(client.disable_acquisition, "disable_acquisition (shutdown)", evt_q)
        except Exception:  # noqa: BLE001 -- belt and braces, _safe_call should already absorb this
            logger.exception("disable_acquisition raised during shutdown")
        try:
            transport.close()
            logger.info(f"disconnected from {port}")
        except Exception:  # noqa: BLE001
            logger.exception("closing the transport raised during shutdown")
        evt_q.put({"type": "disconnected"})


def _safe_call(fn, label: str, evt_q):
    """Runs fn(), reporting an FciError (or any other exception -- see acquisition_worker.py's
    original for why this stays deliberately broad) as an "error" event instead of letting it
    kill this process. Returns _FAILED so callers can tell "got None" (legitimate) from "raised"."""
    try:
        return fn()
    except FciError as e:
        logger.warning(f"{label} failed: {e}")
        evt_q.put({"type": "error", "message": f"{label}: {e}"})
        return _FAILED
    except Exception as e:  # noqa: BLE001 -- see acquisition_worker.py's original _safe_call
        logger.exception(f"{label} raised an unexpected error")
        evt_q.put({"type": "error", "message": f"{label}: {type(e).__name__}: {e}"})
        return _FAILED


def _read_events(client: FciClient, state: dict) -> list:
    """Reads one batch, preferring binary $RQ and falling back to ASCII $RB -- verbatim logic
    from acquisition_worker.py's original _read_events(), `state["binary_batch"]` standing in for
    what used to be an instance attribute."""
    if state["binary_batch"]:
        try:
            return client.read_batch_binary(RB_MAX_BATCH)
        except FciUnknownCommandError:
            logger.info("$RQ not supported by this firmware; using ASCII $RB "
                        "(about half the throughput)")
            state["binary_batch"] = False
    return client.read_batch(min(RB_MAX_BATCH, 32))


def _run_loop(client: FciClient, poll_interval_s: float, stats_interval_s: float,
              busy_interval_s: float, scope_interval_s: float, cmd_q, evt_q) -> None:
    state = {"binary_batch": True}
    scope_running = False
    scope_n = 2048
    last_scope_s = 0.0
    batch_poll_suspended = False
    stats_countdown = 0.0
    poll_wait = 0.0  # first iteration handles pending commands immediately, no wait

    def handle_cmd(msg: dict) -> bool:
        """Returns False if this was the shutdown command, so the caller can stop looping."""
        nonlocal scope_running, scope_n, batch_poll_suspended
        t = msg["type"]
        if t == "shutdown":
            return False
        elif t == "start_acq":
            _safe_call(client.enable_acquisition, "enable_acquisition", evt_q)
            evt_q.put({"type": "acq_state", "running": True})
        elif t == "stop_acq":
            _safe_call(client.disable_acquisition, "disable_acquisition", evt_q)
            evt_q.put({"type": "acq_state", "running": False})
        elif t == "scope_start":
            scope_n = msg["n"]
            scope_running = True
        elif t == "scope_stop":
            scope_running = False
        elif t == "trace_once":
            trace = _safe_call(lambda: client.read_trace(msg["n"]), "read_trace", evt_q)
            if trace is not _FAILED:
                evt_q.put({"type": "trace", "trace": trace})
        elif t == "suspend_batch_polling":
            batch_poll_suspended = True
        elif t == "resume_batch_polling":
            batch_poll_suspended = False
        elif t == "rpc":
            try:
                method = getattr(client, msg["method"])
                value = method(*msg["args"], **msg["kwargs"])
                evt_q.put({"type": "rpc_result", "id": msg["id"], "value": value})
            except Exception as e:  # noqa: BLE001 -- forwarded to the caller's own thread, not
                                     # swallowed; see RemoteFciClient._issue_rpc's re-raise.
                evt_q.put({"type": "rpc_error", "id": msg["id"],
                           "error_type": type(e).__name__, "message": str(e)})
        else:
            logger.warning(f"reader process: unknown command type {t!r}")
        return True

    while True:
        # Block up to poll_wait for the NEXT command rather than sleeping unconditionally: an RPC
        # that arrives mid-wait is handled the instant it lands, which is what keeps config panel
        # Refresh/Apply calls feeling instant across the process boundary (module docstring).
        try:
            msg = cmd_q.get(timeout=poll_wait) if poll_wait > 0 else cmd_q.get_nowait()
            if not handle_cmd(msg):
                return
        except queue.Empty:
            pass
        # Drain anything else that piled up without waiting again.
        try:
            while True:
                if not handle_cmd(cmd_q.get_nowait()):
                    return
        except queue.Empty:
            pass

        if scope_running:
            now = time.monotonic()
            if now - last_scope_s >= scope_interval_s:
                last_scope_s = now
                trace = _safe_call(lambda: client.read_trace(scope_n), "read_trace", evt_q)
                if trace is not _FAILED:
                    evt_q.put({"type": "trace", "trace": trace})

        # Adaptive pacing, verbatim from the original: a full batch means the FIFO had more
        # queued than one request could carry, so poll again at once; a short batch means it's
        # drained, so fall back to the idle period.
        poll_wait = poll_interval_s
        if not batch_poll_suspended:
            events = _safe_call(lambda: _read_events(client, state), "read_batch", evt_q)
            if events is not _FAILED and events:
                evt_q.put({"type": "batch", "events": events})
                if len(events) >= RB_MAX_BATCH:
                    poll_wait = busy_interval_s

        if stats_countdown <= 0:
            stats = _safe_call(client.read_stats, "read_stats", evt_q)
            if stats is not _FAILED:
                evt_q.put({"type": "stats", "stats": stats})
            stats_countdown = stats_interval_s
        stats_countdown -= max(poll_wait, 0.001)
