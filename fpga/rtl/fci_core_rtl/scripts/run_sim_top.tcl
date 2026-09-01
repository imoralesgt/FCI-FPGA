# Integration simulation for fci_core_rtl_top: the full assembled core including the real Xilinx
# FFT IP. Usage:
#   source /tools/Xilinx/Vivado/2022.2/settings64.sh
#   vivado -mode batch -source generate_ip.tcl     # once, to create xfft_2048
#   python3 ../tb/gen_golden.py                    # once, to create stimulus.txt/golden.txt
#   vivado -mode batch -source run_sim_top.tcl
#
# A Vivado project rather than the plain xvhdl/xelab flow in run_sim.sh: the FFT IP's simulation
# model has to be generated and compiled by Vivado, which the standalone flow cannot do. Everything
# that does NOT involve the FFT stays in run_sim.sh, which is much faster to iterate on.

set script_dir [file dirname [file normalize [info script]]]
set core_dir   [file normalize "$script_dir/.."]
set proj_dir   "$script_dir/sim_top_proj"
set sim_dir    "$proj_dir/sim_top_proj.sim/sim_1/behav/xsim"

set xfft_xci [glob -nocomplain "$core_dir/ip/xfft_2048/xfft_2048.xci"]
if {$xfft_xci eq ""} {
  error "xfft_2048.xci not found -- run generate_ip.tcl first"
}
foreach f {stimulus.txt golden.txt} {
  if {![file exists "$core_dir/tb/$f"]} {
    error "$core_dir/tb/$f missing -- run: python3 $core_dir/tb/gen_golden.py"
  }
}

create_project -force sim_top_proj $proj_dir -part xc7a35tcpg236-1

add_files -norecurse [list \
  "$core_dir/src/fci_core_pkg.vhd" \
  "$core_dir/src/bin_accumulator.vhd" \
  "$core_dir/src/result_fifo.vhd" \
  "$core_dir/src/fci_axi4lite_regs.vhd" \
  "$core_dir/src/sample_framer.vhd" \
  "$core_dir/src/fci_core_rtl_top.vhd" \
]
add_files -fileset sim_1 -norecurse "$core_dir/tb/fci_core_rtl_top_tb.vhd"
add_files -norecurse $xfft_xci

set_property file_type {VHDL} [get_files *.vhd]
set_property top fci_core_rtl_top_tb [get_filesets sim_1]
update_compile_order -fileset sources_1
update_compile_order -fileset sim_1

generate_target simulation [get_files $xfft_xci]
export_ip_user_files -of_objects [get_files $xfft_xci] -no_script -force

# The testbench opens stimulus.txt/golden.txt by bare name, so they must sit in xsim's own working
# directory -- which does not exist until the simulation is launched at least once.
file mkdir $sim_dir
file copy -force "$core_dir/tb/stimulus.txt" "$sim_dir/stimulus.txt"
file copy -force "$core_dir/tb/golden.txt"   "$sim_dir/golden.txt"

launch_simulation
run all

puts "INFO: integration simulation finished -- check the transcript above for TEST PASSED/FAILED"

# close_project alone does NOT stop the simulation kernel: launch_simulation starts xsimk as a
# separate socket-attached process, and closing the project just detaches from it, leaving an
# orphan spinning at 100% CPU. The testbench now stops its own clock so "run all" returns, but
# close_sim is what actually reaps the kernel -- keep both.
close_sim -force -quiet
close_project
