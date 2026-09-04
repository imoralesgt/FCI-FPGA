# FCI-FPGA client software

A Python client for the MicroBlaze CLI (`docs/sw/CLI_documentation.md`) and a PySide6 GUI built on
top of it.

## Layout

- `fci_api/` — the protocol client library. Pure Python, no Qt dependency: usable from a plain
  script (see `examples/read_batch_demo.py`) or from any other tool, not just the GUI.
- `gui/` — the PySide6 application: live FCI/PSD viewer, a raw-trace oscilloscope view for setting
  up acquisition parameters, and configuration panels for every subsystem, all built on `fci_api`.
- `examples/` — small standalone scripts demonstrating `fci_api` on its own.

## Running

```
uv run examples/read_batch_demo.py          # auto-detects the board by USB VID:PID
uv run gui/main.py
```

## Spectrum tab: count-rate labels

The Spectrum tab (`gui/ui/histogram_view.py`) shows two count-rate figures next to the total, both
in counts per second (cps):

- **Rate** — instantaneous. A sliding window over the last `RATE_WINDOW_S` seconds (3 s), recomputed
  once a second by a dedicated timer independent of event arrival, so it decays to 0 shortly after
  events stop (Stop pressed, or the device paused) rather than freezing at its last nonzero value.
  Below `RATE_MIN_DT_S` (0.25 s) of actual history in the window it reads 0 rather than dividing by
  a near-zero span, which would otherwise show a spurious spike on the very first batch after a
  gap. Same windowing scheme, same constants' reasoning, as the Live FCI/PSD tab's own per-
  discriminator rate readout (`gui/ui/live_view.py`'s `RATE_WINDOW_S`/`_rate_hz_for()`).
- **Avg** — cumulative. Total counts divided by the elapsed time since the first event after the
  tab was last cleared (`Clear`, or construction). A lifetime average, not windowed: it answers
  "what has the average rate been this run," not "what is happening right now" -- that is Rate's
  job.

Both read 0 until the corresponding condition is met (no events yet, or not enough elapsed time),
never a divide-by-zero.

## Building a standalone executable

```
uv run pyinstaller --onefile --noconsole --name="FCI_Client" gui/main.py
```

Produces a standalone single-executable with embedded libraries under `dist/`. Startup takes a
couple of seconds, but it is the most reliable way to redistribute the application without asking
a lab machine to set up its own Python environment.
