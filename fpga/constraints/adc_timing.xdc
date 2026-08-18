# adc_data[13:0] timing: previously totally unconstrained (no create_clock/set_input_delay at
# all), so Vivado never actually checked this path -- confirmed empirically by opening the
# routed checkpoint (project_fci.runs/impl_1/bd_fci_wrapper_routed.dcp) and applying this same
# constraint in-memory for analysis only: report_timing showed +7.778ns setup slack (comfortable)
# but -0.001ns HOLD slack -- a real, if razor-thin, violation on the currently implemented
# routing. This explains the amplitude-dependent digital corruption seen on large/fast pulses
# (confirmed via oscilloscope + the sibling project on the same board/ADC NOT to be real analog
# behavior, and via the ILA directly on the raw adc_data pin to be upstream of all trigger_core
# RTL): a hold violation is about individual bit transitions landing too close to the capturing
# clock edge -- small/slow signals rarely stress any single bit's timing enough to hit that
# narrow window, but a large fast transition flips many bits at once, raising the odds that at
# least one lands in it. This was never a board defect -- an unconstrained path's routing outcome
# is down to whatever the placer/router happened to do, which can easily differ between projects
# (or resynthesis runs) even on identical hardware.
#
# clk_adc and clk_cpu (which drives trigger_core_0/clk_i, the capturing clock) are both 50MHz
# outputs of the same clk_wiz_0 MMCM (see fci_bd.tcl) -- confirmed via the routed checkpoint that
# Vivado auto-infers a correctly phase-related generated clock for clk_adc from the MMCM primitive
# (clk_adc_bd_fci_clk_wiz_0_0, sourced from .../mmcm_adv_inst/CLKOUT1), so no explicit
# create_generated_clock is needed here -- referencing it by name is sufficient and more robust
# than re-deriving the underlying MMCM pin path by hand.
#
# WHY THIS DEFINES ITS OWN CLOCK RATHER THAN REFERENCING clk_adc_bd_fci_clk_wiz_0_0:
# Three separate fresh resynthesis+reimplementation cycles left INPUT_DELAY empty on every
# adc_data bit, and the third produced a routed checkpoint whose internal Vivado checksum was
# BIT-IDENTICAL to the prior run. The mechanism was then proven directly, outside the GUI
# entirely: opening the synthesized checkpoint and running `read_xdc` on this file by hand showed
# `get_clocks clk_adc_bd_fci_clk_wiz_0_0` returning an EMPTY collection, and INPUT_DELAY still 0
# afterward. clk_adc_bd_fci_clk_wiz_0_0 is an auto-inferred generated clock that only exists once
# the MMCM inside clk_wiz_0 has been linked and Vivado's clock inference has run over it -- but
# at the point design constraints are applied, clk_wiz_0 is still an unlinked out-of-context
# black box. set_input_delay against an empty clock collection silently no-ops instead of
# erroring, so nothing in the log ever flagged it. This was never a file-scoping, staleness, or
# stale-GUI-session problem -- the clock reference itself was simply unresolvable at constraint
# time.
#
# create_clock on the physical adc_clk PORT instead: the port object exists at every stage
# regardless of black boxes, so this always resolves. adc_clk is driven by clk_wiz_0's 50MHz
# clk_adc output (see fci_bd.tcl) and leaves the FPGA to clock the LTC2248, so a 20ns period
# describes it exactly. This makes the ADC source-synchronous input constraint self-contained:
# the ADC returns data referenced to this same clock, which is precisely what set_input_delay
# below needs as its reference.
create_clock -period 20.000 -name adc_clk_ext [get_ports adc_clk]

# Delay budget: LTC2248 clock-to-data delay (tD, CL=5pF, from datasheet): 1.4ns min / 5.4ns max.
# Round-trip board trace (FPGA->ADC clock, ADC->FPGA data) estimated at ~0.3-0.8ns from board
# photos using the CMOD A7's known dimensions as scale -- an approximation pending direct
# measurement from the PCB layout. max = 0.8 + 5.4 = 6.2ns, rounded up to 6.3 for margin;
# min = 0.3 + 1.4 = 1.7ns, rounded down to 1.5 for margin.
set_input_delay -clock adc_clk_ext -max 6.3 [get_ports {adc_data[*]}]
set_input_delay -clock adc_clk_ext -min 1.5 [get_ports {adc_data[*]}] -add_delay
