# Integration simulation for fci_core_rtl_top: the full assembled core including the real Xilinx
# FFT IP. Usage:
#   source /tools/Xilinx/Vivado/2022.2/settings64.sh
#   vivado -mode batch -source generate_ip.tcl     # once, to create xfft_2048
#   vivado -mode batch -source run_sim_top.tcl
#
# A Vivado project rather than the plain xvhdl/xelab flow in run_sim.sh: the FFT IP's simulation
# model has to be generated and compiled by Vivado, which the standalone flow cannot do. Everything
# that does NOT involve the FFT stays in run_sim.sh, which is much faster to iterate on.

set script_dir [file dirname [file normalize [info script]]]
set core_dir   [file normalize "$script_dir/.."]
set proj_dir   "$script_dir/tlast_probe_proj"
set sim_dir    "$proj_dir/tlast_probe_proj.sim/sim_1/behav/xsim"

set xfft_xci [glob -nocomplain "$core_dir/ip/xfft_2048/xfft_2048.xci"]
if {$xfft_xci eq ""} {
  error "xfft_2048.xci not found -- run generate_ip.tcl first"
}

create_project -force tlast_probe_proj $proj_dir -part xc7a35tcpg236-1

add_files -norecurse [list \
  "$core_dir/src/fci_core_pkg.vhd" \
  "$core_dir/src/bin_accumulator.vhd" \
  "$core_dir/src/result_fifo.vhd" \
  "$core_dir/src/fci_axi4lite_regs.vhd" \
  "$core_dir/src/sample_framer.vhd" \
  "$core_dir/src/fci_core_rtl_top.vhd" \
]
add_files -fileset sim_1 -norecurse "$core_dir/tb/xfft_tlast_probe_tb.vhd"
add_files -norecurse $xfft_xci

set_property file_type {VHDL} [get_files *.vhd]
set_property top xfft_tlast_probe_tb [get_filesets sim_1]
update_compile_order -fileset sources_1
update_compile_order -fileset sim_1

generate_target simulation [get_files $xfft_xci]
export_ip_user_files -of_objects [get_files $xfft_xci] -no_script -force

# directory -- which does not exist until the simulation is launched at least once.
file mkdir $sim_dir

launch_simulation
run all

puts "INFO: integration simulation finished -- check the transcript above for TEST PASSED/FAILED"

# close_project alone does NOT stop the simulation kernel: launch_simulation starts xsimk as a
# separate socket-attached process, and closing the project just detaches from it, leaving an
# orphan spinning at 100% CPU. The testbench now stops its own clock so "run all" returns, but
# close_sim is what actually reaps the kernel -- keep both.
close_sim -force -quiet
close_project
