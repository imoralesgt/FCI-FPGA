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
"""How often the acquisition worker calls read_batch(). At the measured ~16 ms round-trip cost,
this leaves comfortable headroom rather than polling flat-out -- read_batch() already amortizes
the round-trip cost across up to 32 events per call, so there is little to gain and UI/CPU cost
to lose by polling faster than this."""
STATS_POLL_INTERVAL_MS = 2000
"""$RS is cheap and only needed for the live health readout, not per-event."""

# --- Reconnection (mirrors the reference's state machine, simplified)
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_INTERVAL_MS = 2000

# --- Storage
BASE_DIR = Path.cwd()
LOG_DIR = BASE_DIR / "logs"
DEFAULT_CSV_DIR = Path.home() / "FciData"
