# Packages fci_core_rtl_top as a Vivado IP-XACT component, replacing BOTH the HLS `fci_core` and
# the separate `fci_sink` cell in the block design. Usage:
#   source /tools/Xilinx/Vivado/2022.2/settings64.sh
#   vivado -mode batch -source generate_ip.tcl      # xfft_2048 must exist first
#   vivado -mode batch -source package_ip.tcl
#
# Follows trigger_core/scripts/package_ip.tcl exactly (same explicit clock/reset association rather
# than trusting auto-detection); the one addition is the generated xfft_2048 .xci, which has to be
# added as a source so the packaged component carries the FFT IP with it.

set script_dir [file dirname [file normalize [info script]]]
set core_dir   [file normalize "$script_dir/.."]
set repo_root  [file normalize "$script_dir/../../../.."]
set ip_out_dir "$repo_root/fpga/ip/fci_core_rtl"
set scratch_proj_dir "$script_dir/fci_core_rtl_pkg_proj"

set xfft_xci [glob -nocomplain "$core_dir/ip/xfft_2048/xfft_2048.xci"]
if {$xfft_xci eq ""} {
  error "xfft_2048.xci not found under $core_dir/ip -- run generate_ip.tcl first"
}

create_project -force fci_core_rtl_pkg_proj $scratch_proj_dir -part xc7a35tcpg236-1

add_files -norecurse [list \
  "$core_dir/src/fci_core_pkg.vhd" \
  "$core_dir/src/bin_accumulator.vhd" \
  "$core_dir/src/result_fifo.vhd" \
  "$core_dir/src/fci_axi4lite_regs.vhd" \
  "$core_dir/src/sample_framer.vhd" \
  "$core_dir/src/fci_core_rtl_top.vhd" \
]
set_property file_type {VHDL} [get_files *.vhd]

add_files -norecurse $xfft_xci

set_property top fci_core_rtl_top [current_fileset]
update_compile_order -fileset sources_1

file mkdir $ip_out_dir
ipx::package_project -root_dir $ip_out_dir -vendor FCI-FPGA -library user -taxonomy /UserIP \
  -import_files -set_current true

set core [ipx::current_core]
set_property name fci_core_rtl $core
set_property display_name {FCI Core (RTL, 2048-point)} $core
set_property description \
  {Frequency Classification Index core: 2048-point FFT (Xilinx xfft, block floating point, bit-reversed output) over trigger_core's captured trace, accumulating |Re|+|Im| over two runtime-programmable bin windows, with a 32-deep buffered AXI4-Lite result window. Replaces the Vitis HLS fci_core (whose ap_ufixed<18,2> bin magnitude truncated away the discrimination signal) and absorbs fci_sink. Registers: psa_l_lo/hi (0x00/0x04), psa_w_lo/hi (0x08/0x0C), ctrl (0x10), status (0x14), psa_l/psa_w (0x18/0x1C), timestamp (0x20/0x24), event_count (0x28), watermark (0x2C).} \
  $core
set_property vendor_display_name {FCI-FPGA project} $core
set_property version 1.0 $core

# --- Verify/fix bus interface inference rather than trust it blindly ---

set bus_ifs [ipx::get_bus_interfaces -of_objects $core]
puts "INFO: Inferred bus interfaces: $bus_ifs"

if {[llength [ipx::get_bus_interfaces -of_objects $core clk_i]] == 0} {
  ipx::add_bus_interface clk_i $core
  set clk_if [ipx::get_bus_interfaces clk_i -of_objects $core]
  set_property abstraction_type_vlnv xilinx.com:signal:clock_rtl:1.0 $clk_if
  set_property bus_type_vlnv xilinx.com:signal:clock:1.0 $clk_if
  ipx::add_port_map CLK $clk_if
  set_property physical_name clk_i [ipx::get_port_maps CLK -of_objects $clk_if]
}

if {[llength [ipx::get_bus_interfaces -of_objects $core rstn_i]] == 0} {
  ipx::add_bus_interface rstn_i $core
  set rst_if [ipx::get_bus_interfaces rstn_i -of_objects $core]
  set_property abstraction_type_vlnv xilinx.com:signal:reset_rtl:1.0 $rst_if
  set_property bus_type_vlnv xilinx.com:signal:reset:1.0 $rst_if
  ipx::add_port_map RST $rst_if
  set_property physical_name rstn_i [ipx::get_port_maps RST -of_objects $rst_if]
  ipx::add_bus_parameter POLARITY $rst_if
  set_property value ACTIVE_LOW [ipx::get_bus_parameters POLARITY -of_objects $rst_if]
}

foreach axi_if {s_axi s_axis} {
  if {[llength [ipx::get_bus_interfaces $axi_if -of_objects $core]] > 0} {
    ipx::associate_bus_interfaces -busif $axi_if -clock clk_i $core
  }
}

set clk_if [ipx::get_bus_interfaces clk_i -of_objects $core]
ipx::add_bus_parameter ASSOCIATED_RESET $clk_if
set_property value rstn_i [ipx::get_bus_parameters ASSOCIATED_RESET -of_objects $clk_if]

set_property supported_families {artix7 Production} $core

ipx::create_xgui_files $core
ipx::update_checksums $core
ipx::save_core $core

puts "INFO: Final bus interfaces after fixup: [ipx::get_bus_interfaces -of_objects $core]"
puts "INFO: IP packaged at $ip_out_dir"

close_project
