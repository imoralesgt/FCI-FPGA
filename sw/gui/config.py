"""Constants for the GUI application. Mirrors NSIL-Counter's config.py in spirit -- hardware
identification, timing, and storage paths in one place -- adapted to this project's protocol
(fci_api owns the wire-level constants like baud rate; this file only holds GUI-level concerns).
"""

from pathlib import Path

# --- Hardware target filtering (Digilent FT2232H bridge -- confirmed via `udevadm` this session)
TARGET_VID_HEX = "0403"
TARGET_PID_HEX = "6010"

# --- Live acquisition polling
BATCH_POLL_INTERVAL_MS = 200
"""Idle poll period for read_batch(). This is the BACKOFF value, not the working one -- see
BATCH_POLL_BUSY_MS. The original reasoning here ("little to gain by polling faster") was wrong:
$RB returns at most RB_MAX_BATCH = 32 events, because that is the hardware result FIFO's depth, so
a fixed 200 ms period hard-caps live throughput at 5 x 32 = 160 events/s no matter what the
detector is doing. Measured on hardware 2026-09-01: 147 events/s with the scope off, 107 with it
on, against a source the FPGA was processing at ~970 events/s."""
BATCH_POLL_BUSY_MS = 0
"""Poll period once a batch comes back FULL (RB_MAX_BATCH events), i.e. the result FIFO had more
queued than one request could carry. Zero means "immediately", which is correct rather than
aggressive: a full batch is direct evidence events are being lost, and the round trip itself
already paces the loop -- at 4 Mbaud a full 1024-record binary frame is ~64 ms of wire time. The
loop falls back to BATCH_POLL_INTERVAL_MS as soon as a batch comes back short, so an idle
instrument costs the same as before."""
SCOPE_INTERVAL_MS = 500
"""How often continuous ("Start") scope mode requests a trace. NOT a render setting -- it is a
bandwidth budget. A `$RT 2048` reply is ~11 kB of ASCII, 28 ms of wire time at 4 Mbaud, so
requesting one per worker iteration (~30/s once adaptive polling removed the fixed delay) consumed
~338 kB/s of a 400 kB/s link and starved event readout of most of it. At 2 Hz it costs ~23 kB/s,
about 6%.

2 Hz is also plenty to watch a pulse shape by: the scope exists to set acquisition parameters, not
to catch individual events, and traces are single captures rather than a continuous waveform, so a
faster refresh shows different events rather than a smoother picture."""

STATS_POLL_INTERVAL_MS = 2000
"""$RS is cheap and only needed for the live health readout, not per-event."""

# --- Reconnection (mirrors the reference's state machine, simplified)
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_INTERVAL_MS = 2000

# --- Storage
BASE_DIR = Path.cwd()
LOG_DIR = BASE_DIR / "logs"
DEFAULT_CSV_DIR = Path.home() / "FciData"
