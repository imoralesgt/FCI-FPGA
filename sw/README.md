# FCI-FPGA client software

A Python client for the MicroBlaze CLI (`docs/CLI_documentation.md`) and a PySide6 GUI built on
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

## Building a standalone executable

```
uv run pyinstaller --onefile --noconsole --name="FCI_Client" gui/main.py
```

Produces a standalone single-executable with embedded libraries under `dist/`. Startup takes a
couple of seconds, but it is the most reliable way to redistribute the application without asking
a lab machine to set up its own Python environment.
