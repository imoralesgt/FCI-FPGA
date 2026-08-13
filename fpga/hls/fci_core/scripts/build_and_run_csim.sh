#!/usr/bin/env bash
# Builds and runs the csim.exe that `csim_design -setup` (in run_hls.tcl) only generates
# makefiles/sources for, instead of vitis_hls's own internal build.
#
# Why this is needed: this host's system glibc is much newer than the toolchain Vitis HLS
# 2022.2 bundles for csim, and links against it fail (`unknown type [0x13] section .relr.dyn`
# from the bundled ld). Neither exporting AP_GCC_PATH nor setting it via `set ::env(...)` in the
# tcl script gets through to whatever environment vitis_hls's internal csim launcher actually
# uses, so this script builds/runs it directly instead, forcing the system gcc/ld (confirmed
# working) via AP_GCC_PATH, and setting LD_LIBRARY_PATH for the Xilinx bit-accurate C-models
# (FFT, floating-point, etc.) that csim.exe dynamically links against.
set -euo pipefail

: "${XILINX_HLS:?Source Vitis_HLS settings64.sh first}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/fci_core_prj/solution1/csim/build"

LD_LIBRARY_PATH="$XILINX_HLS/lnx64/tools/fft_v9_1:$XILINX_HLS/lnx64/tools/fft_v9_0:$XILINX_HLS/lnx64/tools/fpo_v7_1:$XILINX_HLS/lnx64/tools/fir_v7_0:$XILINX_HLS/lnx64/tools/dds_v6_0:${LD_LIBRARY_PATH:-}"
export LD_LIBRARY_PATH

make -C "$BUILD_DIR" -f csim.mk AP_GCC_PATH=/usr/bin csim.exe
"$BUILD_DIR/csim.exe"
