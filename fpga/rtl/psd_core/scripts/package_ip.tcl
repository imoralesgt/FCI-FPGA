# Packages psd_core_top as a Vivado IP-XACT component, mirroring trigger_core/scripts/package_ip.tcl.
# Usage:
#   source /tools/Xilinx/Vivado/2022.2/settings64.sh && vivado -mode batch -source package_ip.tcl
#
# Clock/reset association and rstn_i's active-low polarity are set explicitly rather than trusted
# to auto-detection, and the resulting interface list is printed so it can be checked rather than
# assumed.

set script_dir [file dirname [file normalize [info script]]]
set core_dir   [file normalize "$script_dir/.."]
set repo_root  [file normalize "$script_dir/../../../.."]
set ip_out_dir "$repo_root/fpga/ip/psd_core"
set scratch_proj_dir "$script_dir/psd_core_pkg_proj"

create_project -force psd_core_pkg_proj $scratch_proj_dir -part xc7a35tcpg236-1

add_files -norecurse [list \
  "$core_dir/src/psd_core_pkg.vhd" \
  "$core_dir/src/dual_gate_integrator.vhd" \
  "$core_dir/src/result_fifo.vhd" \
  "$core_dir/src/psd_axi4lite_regs.vhd" \
  "$core_dir/src/psd_core_top.vhd" \
]
set_property file_type {VHDL} [get_files *.vhd]
set_property top psd_core_top [current_fileset]
update_compile_order -fileset sources_1

file mkdir $ip_out_dir
ipx::package_project -root_dir $ip_out_dir -vendor FCI-FPGA -library user -taxonomy /UserIP \
  -import_files -set_current true

set core [ipx::current_core]
set_property name psd_core $core
set_property display_name {Pulse-Shape Discrimination Core} $core
set_property description {CAEN-style dual-gate charge integrator. Consumes the framed trace from trigger_core and produces ENERGY_SHORT and ENERGY (long) per event, tagged with the 64-bit TUSER timestamp, buffered in a 32-deep result FIFO readable over AXI4-Lite with a watermark interrupt. Never backpressures the stream. AXI4-Lite: pre_trigger (0x00), pre_gate (0x04), short_gate (0x08), long_gate (0x0C), baseline_ref (0x10), ctrl (0x14), status (0x18), results (0x1C-0x2C), watermark (0x30).} $core
set_property vendor_display_name {FCI-FPGA project} $core
set_property version 1.0 $core

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
