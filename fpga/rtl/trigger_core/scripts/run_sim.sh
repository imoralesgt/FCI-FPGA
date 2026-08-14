#!/usr/bin/env bash
# Compiles and runs trigger_core_tb via xvhdl/xelab/xsim (pure VHDL, no HLS toolchain issues).
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
  "$SRC_DIR/trigger_core_pkg.vhd" \
  "$SRC_DIR/delay_line.vhd" \
  "$SRC_DIR/trigger.vhd" \
  "$SRC_DIR/circular_buffer.vhd" \
  "$SRC_DIR/axi4lite_regs.vhd" \
  "$SRC_DIR/capture_engine.vhd" \
  "$SRC_DIR/trigger_core_top.vhd" \
  "$TB_DIR/trigger_core_tb.vhd"

xelab --93_mode --debug typical trigger_core_tb -s trigger_core_tb_sim

xsim trigger_core_tb_sim -runall
