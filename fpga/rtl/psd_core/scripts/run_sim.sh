#!/usr/bin/env bash
# Compiles and runs psd_core_tb via xvhdl/xelab/xsim (pure VHDL).
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
  "$SRC_DIR/psd_core_pkg.vhd" \
  "$SRC_DIR/dual_gate_integrator.vhd" \
  "$SRC_DIR/result_fifo.vhd" \
  "$SRC_DIR/psd_axi4lite_regs.vhd" \
  "$SRC_DIR/psd_core_top.vhd" \
  "$TB_DIR/psd_core_tb.vhd"

xelab --93_mode --debug typical psd_core_tb -s psd_core_tb_sim

xsim psd_core_tb_sim -runall
