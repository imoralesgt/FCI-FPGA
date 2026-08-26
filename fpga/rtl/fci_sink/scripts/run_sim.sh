#!/usr/bin/env bash
# Compiles and runs fci_sink_tb via xvhdl/xelab/xsim (pure VHDL).
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
  "$SRC_DIR/fci_sink_pkg.vhd" \
  "$SRC_DIR/beat_pair_collector.vhd" \
  "$SRC_DIR/result_fifo.vhd" \
  "$SRC_DIR/fci_sink_axi4lite_regs.vhd" \
  "$SRC_DIR/fci_sink_top.vhd" \
  "$TB_DIR/fci_sink_tb.vhd"

xelab --93_mode --debug typical fci_sink_tb -s fci_sink_tb_sim

xsim fci_sink_tb_sim -runall
