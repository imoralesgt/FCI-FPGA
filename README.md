# FCI-FPGA

FPGA deployment of the gamma/neutron discrimination method based on frequency components analysis
(FCI), alongside a conventional charge-comparison PSD path, on a CMOD-A7 (Artix-7 XC7A35T) with a
SiPM-ready analog front end. A MicroBlaze soft core runs the acquisition firmware and exposes an
ASCII/binary CLI over UART; a PySide6 GUI (or any script against the Python client library) drives
it from a host PC.

The method is based on an FFT magnitude spectrum reduced to a ratio of two frequency bands, in place
of (or alongside) charge comparison, as described in Morales et al., *"Digital pulse
shape discrimination method based on frequency components analysis for CLYC detector"* (Nucl.
Instrum. Methods Phys. Res. A, 2023): this project is that method's real-time FPGA implementation
and hardware validation, run against a CLYC scintillator with a DD neutron generator, Cs-137 and
Co-60 sources.

## Block diagram

```mermaid
flowchart LR
    ADC[["ADC\n(AFE, 50 Msps)"]] --> BLR["blr_core\nbaseline restorer"]
    BLR -- AXI-Stream --> TRIG["trigger_core\nCFD trigger + capture window"]
    TRIG -- AXI-Stream --> CDC[["CDC FIFO\nADC clk -> CPU clk"]]
    CDC --> BCAST{{"AXI-Stream\nbroadcaster"}}
    BCAST --> DMA["AXI DMA (S2MM)\nraw trace capture"]
    BCAST --> PSD["psd_core\ncharge comparison:\n(long-short)/long"]
    BCAST --> FCI["fci_core_rtl\n2048-pt FFT -> ASDM\nPSA_l / PSA_w"]

    DMA -.->|"BRAM"| MB[["MicroBlaze\nacquisition firmware"]]
    PSD -- "AXI-Lite" --- MB
    FCI -- "AXI-Lite" --- MB
    TRIG -- "AXI-Lite" --- MB
    BLR -- "AXI-Lite" --- MB
    MB -- "UART, 4 Mbaud" --- HOST(["Host PC\nfci_api / GUI"])

    style ADC fill:#eef,stroke:#557
    style MB fill:#fee,stroke:#755
    style HOST fill:#efe,stroke:#575
```

Every block above is real RTL/IP in this repository (`fpga/rtl/`) -- see [`fpga/bd/fci_bd.tcl`](fpga/bd/fci_bd.tcl) for the Vivado block design
this diagram summarizes, or the exported canvas:

[![FPGA block design (Vivado)](docs/log/images/fpga_block_design.svg)](docs/log/images/fpga_block_design.svg)

FCI and PSD run **in parallel** off the same broadcast trace, each independently paired by
timestamp on the firmware side (`fci_api`'s `AcqEvent` carries both `fci` and `psd` for every
event) -- neither is a fallback for the other; the point of this project is comparing them
directly against the same pulses.

## Repository layout

```
fpga/
  rtl/            trigger_core, blr_core, psd_core, fci_core_rtl, fci_sink -- one dir per IP core,
                  each with its own src/tb/scripts
  hls/            fci_core's HLS source (pre-RTL-migration path; see docs/log for the migration)
  bd/             the Vivado block design as a reproducible .tcl script (fci_bd.tcl)
  ublaze_sw/      MicroBlaze firmware: acquisition, CLI, per-subsystem drivers (psd.c, blr.c, ...)
  projects/       where the Vivado/Vitis projects live once created (gitignored) -- see "Building
                  the bitstream and firmware" below for how to recreate them from ip/, constraints/
                  and bd/
  bitstream/      pre-built outputs: XSA export from Vivado and the bitstream merged with the
                  compiled MicroBlaze firmware (download.bit)
sw/
  fci_api/        pure-Python protocol client (no Qt dependency) -- framing, retries, one typed
                  method per CLI command
  gui/            the PySide6 application described below
  analysis/       offline validation scripts against recorded raw traces
  examples/       small standalone scripts against fci_api
docs/
  sw/             GUI user interface + full CLI wire protocol
  log/            the project's running development log (design decisions, bring-up debugging,
                  experimental validation results)
  fpga/           AFE schematics and ADC datasheet
```

## The GUI

![Live FCI/PSD tab](docs/sw/images/live_fci_psd_tab_scatter.png)

A project-based workflow modeled on similar commercial solutions: a project is a folder holding one campaign's
instrument settings *and* its recorded data together (`settings.json`, `RAW/`, `LIST/`,
`SPECTRA/`), and nothing else in the window -- not even Connect -- is usable until one is open.
Five tabs cover file naming, per-subsystem configuration, the trigger/oscilloscope view, a live
energy spectrum with SPE export, and the live FCI-vs-Energy / PSD-vs-Energy discrimination plots
shown above.

![No project open -- everything is locked until one is created or opened](docs/sw/images/no_project_locked.png)

See [`docs/sw/README.md`](docs/sw/README.md) for the full tour (every tab, with screenshots) and
[`docs/sw/CLI_documentation.md`](docs/sw/CLI_documentation.md) for the wire protocol itself.

## Running

```
cd sw
uv run examples/read_batch_demo.py          # auto-detects the board by USB VID:PID
uv run gui/main.py
```

## Building the bitstream and firmware

Neither the Vivado project nor the Vitis workspace is checked in (see `.gitignore`'s
`project_*`/`fpga/**/project_*/` rules) -- what's committed instead is the minimum needed to
*recreate* them: the packaged IP cores (`fpga/ip/`), constraints (`fpga/constraints/`), and the
block design as a reproducible Tcl script (`fpga/bd/fci_bd.tcl`). A pre-built result of this flow
(XSA export plus the bitstream already merged with the compiled MicroBlaze firmware) ships in
`fpga/bitstream/` for anyone who just wants to program the board.

**Vivado (2022.2):**
1. Create a new RTL project targeting the CMOD-A7 (XC7A35T), with `fpga/ip/` added as a local IP
   repository so the block design's custom cells (`blr_core`, `trigger_core`, `psd_core`,
   `fci_core_rtl`, `fci_sink`) resolve.
2. Add `fpga/constraints/*.xdc`.
3. Source `fpga/bd/fci_bd.tcl` to rebuild the block design, then Generate Bitstream.
4. Export the hardware platform (`File > Export > Export Hardware`, include bitstream) to get a
   fresh `.xsa` -- or reuse `fpga/bitstream/bd_fci_wrapper.xsa` if the block design hasn't changed.

**Vitis:** create a new workspace from that XSA, then a platform + application project against it,
and copy the sources under `fpga/ublaze_sw/` into the application project's `src/`.

Build the application in **Release**, not Debug -- MicroBlaze is allocated a 64 KB LMB block RAM in
this design, and Debug's larger stack/heap footprint and lack of optimization do not fit; only a
Release (`-Os`) build links within that budget.

Program the board with the resulting `.bit`/`.elf` pair (or `updatemem` the ELF into the exported
bitstream, as `fpga/bitstream/download.bit` already is) to get a single bitstream that boots
straight into the acquisition firmware.

## Documentation

- [`docs/sw/README.md`](docs/sw/README.md) — GUI tour, tab by tab, with screenshots
- [`docs/sw/CLI_documentation.md`](docs/sw/CLI_documentation.md) — UART CLI wire protocol
- [`sw/README.md`](sw/README.md) — Python client/GUI developer quick-start
- [`docs/log/README.md`](docs/log/README.md) — running development log: design rationale, bring-up
  debugging, and experimental validation (DD/Cs-137/Co-60) against the Morales et al. method
