# Out-of-context synthesis of the two trigger mechanisms, so "which costs more" is measured
# rather than estimated. Usage:
#   source /tools/Xilinx/Vivado/2022.2/settings64.sh
#   vivado -mode batch -source compare_trigger_area.tcl
#
# OOC (-mode out_of_context) synthesises the module alone with no I/O buffers, which is what makes
# the two comparable: a top-level synthesis would bury both in the rest of trigger_core.

set script_dir [file dirname [file normalize [info script]]]
set src        [file normalize "$script_dir/../src"]
set part       xc7a35tcpg236-1

proc area_of {label files top generics part} {
    create_project -in_memory -part $part
    foreach f $files { read_vhdl -vhdl2008 $f }
    set args [list -top $top -mode out_of_context -part $part]
    foreach g $generics { lappend args -generic $g }
    synth_design {*}$args
    # Count primitives directly rather than parsing report_utilization's table.
    set n_lut  [llength [get_cells -hier -filter {PRIMITIVE_GROUP == LUT}]]
    set n_ff   [llength [get_cells -hier -filter {PRIMITIVE_GROUP == FLOP_LATCH}]]
    # PRIMITIVE_GROUP for a DSP48E1 is MULT, not DSP -- filtering on DSP silently reported 0 and
    # made a variant that does infer a multiplier look identical to one that cannot.
    set n_dsp  [llength [get_cells -hier -filter {PRIMITIVE_GROUP == MULT}]]
    # One SRL per BIT LANE, so this counts data width, not delay depth; depth shows up as
    # SRLC16E vs SRLC32E, not as a different count.
    set n_srl  [llength [get_cells -hier -filter {PRIMITIVE_SUBGROUP == srl}]]
    set n_bram [llength [get_cells -hier -filter {PRIMITIVE_GROUP == BLOCKRAM}]]
    set carry [llength [get_cells -hier -filter {PRIMITIVE_GROUP == CARRY}]]
    puts "AREA_RESULT $label LUT=$n_lut FF=$n_ff DSP=$n_dsp SRL=$n_srl CARRY=$carry BRAM=$n_bram"
    close_project
}

set pkg  "$src/trigger_core_pkg.vhd"
set cfd  "$src/cfd_trigger.vhd"
set wrap "$script_dir/area_wrappers.vhd"

# Configurations are pinned by WRAPPERS with explicit generic maps, not by -generic: Vivado
# silently ignored -generic for this entity (DLY_MAX=8 still synthesised 16 SRLs, identical to
# the default), which made every variant come out the same and the comparison worthless.
area_of "cross_level"    [list $pkg "$src/trigger.vhd"] trigger        {} $part
area_of "cfd_prog_frac"  [list $pkg $cfd $wrap]         cfd_area_dsp   {} $part
area_of "cfd_half_frac"  [list $pkg $cfd $wrap]         cfd_area_shift {} $part
area_of "cfd_short_dly"  [list $pkg $cfd $wrap]         cfd_area_short {} $part

puts "DONE"
