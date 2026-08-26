#!/usr/bin/env bash
# Pure-VHDL portion of the fci_core_rtl testbenches: everything except the FFT IP itself, which
# needs a Vivado project (see scripts/run_sim_top.tcl).
# Usage: source /tools/Xilinx/Vivado/2022.2/settings64.sh && ./run_sim.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/../src"
TB_DIR="$SCRIPT_DIR/../tb"
WORK_DIR="$SCRIPT_DIR/xsim_work"

: "${XILINX_VIVADO:?Source Vivado settings64.sh first}"

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

xvhdl --93_mode \
  "$SRC_DIR/fci_core_pkg.vhd" \
  "$SRC_DIR/bin_accumulator.vhd" \
  "$TB_DIR/bin_accumulator_tb.vhd"

xelab --93_mode --debug typical bin_accumulator_tb -s bin_accumulator_tb_sim

xsim bin_accumulator_tb_sim -runall
