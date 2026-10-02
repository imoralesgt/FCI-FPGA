#!/usr/bin/env bash
# Compiles and runs trace_tagger_tb via xvhdl/xelab/xsim (pure VHDL).
# Usage: source /tools/Xilinx/Vivado/2022.2/settings64.sh && ./run_sim.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/../src"
TB_DIR="$SCRIPT_DIR/../tb"
WORK_DIR="$SCRIPT_DIR/xsim_work"

: "${XILINX_VIVADO:?Source Vivado settings64.sh first}"

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

xvhdl --2008 \
  "$SRC_DIR/trace_tagger.vhd" \
  "$TB_DIR/trace_tagger_tb.vhd"

xelab --debug typical trace_tagger_tb -s trace_tagger_tb_sim

xsim trace_tagger_tb_sim -runall
