# Generates the Xilinx FFT LogiCORE (xfft v9.1) instance this core wraps.
#
# Run before simulating or packaging:
#   vivado -mode batch -source scripts/generate_ip.tcl
#
# Every CONFIG value below was verified against the real IP in Vivado 2022.2 rather than copied
# from documentation -- `aresetn` in particular is NOT `has_aresetn` (which the IP rejects), and
# `bit_reversed_order` happens to already be the IP's default.
#
# Where these came from: the HLS core this replaces embedded its own xfft instance, and its .xci
# (fci_core_prj/.../fci_core_fft_syn_fci_fft_config_s_core_ip.xci) is the reference for everything
# here EXCEPT the two deliberate changes:
#
#   transform_length  1024 -> 2048   The detector's pulse has a very low spectral corner (tau ~1.4us
#                                    puts it near bin 2-3 of 1024 at 50 Msps). Doubling the transform
#                                    halves bin spacing to ~24.4 kHz, putting more resolution exactly
#                                    where the shape information actually lives. Window is 40.96 us.
#   output_ordering   natural ->     Natural ordering costs the IP a reorder buffer (~1 RAMB36 here)
#                     bit_reversed   purely to permute the output stream. bin_accumulator.vhd undoes
#                                    the permutation with a wire reversal instead -- no logic, no
#                                    latency, no memory. On a device already at ~81% BRAM that is a
#                                    real saving. See fci_core_pkg.vhd's bit_reverse().
#
# scaling_options = block_floating_point is kept from the HLS config: the FFT reports one shared
# exponent per frame, and since FCI is a RATIO of two sums over the SAME frame, that exponent
# cancels exactly and never has to be applied. (Note this is not what caused the precision problem
# that motivated this rewrite -- that was the HLS core truncating each bin magnitude to an
# ap_ufixed<18,2>. This core accumulates full-width; see bin_accumulator.vhd.)

set script_dir [file dirname [file normalize [info script]]]
set core_dir   [file dirname $script_dir]
set ip_dir     $core_dir/ip
set proj_dir   $script_dir/ip_gen_proj

file mkdir $ip_dir

create_project -force fci_core_rtl_ip_gen $proj_dir -part xc7a35tcpg236-1

create_ip -name xfft -vendor xilinx.com -library ip -version 9.1 \
  -module_name xfft_2048 -dir $ip_dir

set_property -dict [list \
  CONFIG.transform_length {2048} \
  CONFIG.target_data_throughput {50} \
  CONFIG.run_time_configurable_transform_length {false} \
  CONFIG.data_format {fixed_point} \
  CONFIG.input_width {16} \
  CONFIG.phase_factor_width {16} \
  CONFIG.scaling_options {block_floating_point} \
  CONFIG.output_ordering {bit_reversed_order} \
  CONFIG.butterfly_type {use_luts} \
  CONFIG.rounding_modes {convergent_rounding} \
  CONFIG.aresetn {true} \
] [get_ips xfft_2048]

generate_target {instantiation_template synthesis simulation} [get_ips xfft_2048]

puts "xfft_2048 generated under $ip_dir"
close_project
