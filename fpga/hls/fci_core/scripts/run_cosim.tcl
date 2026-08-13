# RTL/C-RTL cosimulation for fci_core, run separately from run_hls.tcl since it's slow
# (generates and simulates the actual FFT netlist). Requires run_hls.tcl to have been run first
# (reuses its project/solution). Usage: vitis_hls -f run_cosim.tcl
set ::env(AP_GCC_PATH) /usr/bin
# cosim.tv.exe is compiled/linked with this host's system g++/libstdc++, but Vitis HLS's own
# bundled (older) libstdc++.so.6 shadows it at runtime otherwise (GLIBCXX_3.4.32 etc. not found).
if {[info exists ::env(LD_LIBRARY_PATH)]} {
    set ::env(LD_LIBRARY_PATH) "/usr/lib/x86_64-linux-gnu:$::env(LD_LIBRARY_PATH)"
} else {
    set ::env(LD_LIBRARY_PATH) "/usr/lib/x86_64-linux-gnu"
}

open_project fci_core_prj
open_solution "solution1"
cosim_design -trace_level all
exit
