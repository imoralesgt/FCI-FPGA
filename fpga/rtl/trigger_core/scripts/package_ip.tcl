# Packages trigger_core_top as a Vivado IP-XACT component (IP repository entry), so it can be
# added to the block design's IP catalog alongside fci_core. Usage:
#   source /tools/Xilinx/Vivado/2022.2/settings64.sh && vivado -mode batch -source package_ip.tcl
#
# Bus interfaces (s_axi_*, m_axis_t*) are inferred automatically from the standard AXI4-Lite/
# AXI4-Stream port naming already used in trigger_core_top.vhd. Clock/reset association and
# rstn_i's active-low polarity are set explicitly below rather than trusted to auto-detection,
# and verified by inspecting the packaged component afterwards.

set script_dir [file dirname [file normalize [info script]]]
set core_dir   [file normalize "$script_dir/.."]
set repo_root  [file normalize "$script_dir/../../../.."]
set ip_out_dir "$repo_root/fpga/ip/trigger_core"
set scratch_proj_dir "$script_dir/trigger_core_pkg_proj"

create_project -force trigger_core_pkg_proj $scratch_proj_dir -part xc7a35tcpg236-1

add_files -norecurse [list \
  "$core_dir/src/trigger_core_pkg.vhd" \
  "$core_dir/src/delay_line.vhd" \
  "$core_dir/src/trigger.vhd" \
  "$core_dir/src/circular_buffer.vhd" \
  "$core_dir/src/axi4lite_regs.vhd" \
  "$core_dir/src/capture_engine.vhd" \
  "$core_dir/src/trigger_core_top.vhd" \
]
set_property file_type {VHDL} [get_files *.vhd]
set_property top trigger_core_top [current_fileset]
update_compile_order -fileset sources_1

file mkdir $ip_out_dir
ipx::package_project -root_dir $ip_out_dir -vendor FCI-FPGA -library user -taxonomy /UserIP \
  -import_files -set_current true

set core [ipx::current_core]
set_property name trigger_core $core
set_property display_name {Cross-Level Trigger Core} $core
set_property description \
  {Cross-level trigger with pre-trigger delay-line lookback (2-256 samples) and triggered-capture AXI4-Stream output (up to 4096 samples), matching fci_core's input format. AXI4-Lite registers: threshold (0x00), polarity (0x04), delay (0x08), depth (0x0C).} \
  $core
set_property vendor_display_name {FCI-FPGA project} $core
set_property version 1.0 $core

# --- Verify/fix bus interface inference rather than trust it blindly ---

set bus_ifs [ipx::get_bus_interfaces -of_objects $core]
puts "INFO: Inferred bus interfaces: $bus_ifs"

# Clock: associate clk_i as the clock for both the AXI4-Lite and AXI4-Stream interfaces.
if {[llength [ipx::get_bus_interfaces -of_objects $core clk_i]] == 0} {
  ipx::add_bus_interface clk_i $core
  set clk_if [ipx::get_bus_interfaces clk_i -of_objects $core]
  set_property abstraction_type_vlnv xilinx.com:signal:clock_rtl:1.0 $clk_if
  set_property bus_type_vlnv xilinx.com:signal:clock:1.0 $clk_if
  ipx::add_port_map CLK $clk_if
  set_property physical_name clk_i [ipx::get_port_maps CLK -of_objects $clk_if]
}

# Reset: associate rstn_i, active-low.
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

# Associate clk_i/rstn_i with the AXI interfaces so downstream tools (block automation) know
# which clock/reset domain they belong to.
foreach axi_if {s_axi m_axis} {
  if {[llength [ipx::get_bus_interfaces $axi_if -of_objects $core]] > 0} {
    ipx::associate_bus_interfaces -busif $axi_if -clock clk_i $core
  }
}

# Tell tools (e.g. block-design connection automation) which reset goes with clk_i.
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
