-- RETIRED 2026-09-02: replaced by cfd_trigger.vhd in trigger_core_top.
--
-- Kept in the tree, not deleted, because scripts/compare_trigger_area.tcl synthesises it as the
-- area baseline for the CFD (22 LUT / 17 FF here against 91 LUT / 26 FF / 1 DSP / 16 SRL there),
-- and because cfd_trigger_tb scores the CFD's amplitude walk against what this would have done on
-- the same pulses. It is not instantiated by any design.
--
-- Cross-level trigger: watches the live (undelayed) ADC sample and produces a single-cycle
-- trigger_o pulse when it crosses threshold_i, in the direction selected by polarity_i.
--
-- polarity_i = '1': trigger on a rising crossing (signal goes from below threshold to
--   at-or-above threshold) -- for positive-going pulses.
-- polarity_i = '0': trigger on a falling crossing (signal goes from at-or-above threshold to
--   below threshold) -- for negative-going pulses. Note the digitised pulses on this detector go
--   UP from baseline (project log, section 7), so normal operation is polarity_i = '1' with a
--   small positive threshold, despite the analogue pulse at the AFE output being negative-going.
--
-- Sample data is SIGNED, centred on zero by blr_core upstream, so the comparison is a signed
-- compare and threshold_i is a signed level. That matters for more than tidiness: with a restored
-- baseline at zero, a physically sensible threshold for this detector's positive-going pulses is a
-- small positive number, and the useful part of the range sits either side of zero. An unsigned
-- compare would read every negative sample -- half the noise distribution -- as a huge positive
-- value, so the comparator would sit permanently "above" and never produce an edge.
-- armed_i gates trigger_o so a new trigger can't fire while a capture is already in progress.
--
-- Reconfiguration hazard: `above` is the registered "was above threshold as of the previous
-- clock edge" state, computed under whatever threshold_i/polarity_i were *then*. If threshold_i
-- or polarity_i changes, `above_now` on the very next cycle is computed under the NEW
-- configuration while `above` still reflects the OLD one -- a difference that can look exactly
-- like a genuine crossing even though adc_data_i never moved. Confirmed on real hardware: this is
-- what was producing "triggered" captures full of pure baseline noise every time firmware
-- reprogrammed threshold_i. Fixed by registering the previous threshold_i/polarity_i and
-- suppressing trigger_o for exactly the one cycle where either changed, while still updating
-- `above` so the very next cycle onward compares correctly against the new configuration.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity trigger is
  generic (
    DATA_WIDTH : integer := 14
  );
  port (
    clk_i       : in  std_logic;
    rstn_i      : in  std_logic;
    armed_i     : in  std_logic;
    adc_data_i  : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    threshold_i : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    polarity_i  : in  std_logic;
    trigger_o   : out std_logic
  );
end entity trigger;

architecture rtl of trigger is

  -- Registered "was above threshold as of the previous clock edge" state.
  signal above : std_logic;

  -- Previous cycle's threshold_i/polarity_i, to detect a live reconfiguration and suppress the
  -- false edge it would otherwise produce (see header comment).
  signal threshold_q : std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal polarity_q  : std_logic;

begin

  -- above_now is a variable (not a signal) specifically so it's usable immediately within this
  -- same process for edge detection against the signal `above`, which still holds last cycle's
  -- value at that point (VHDL signal assignments only take effect at the next clock edge, so
  -- comparing against `above` here correctly reads the pre-update, previous-cycle state).
  process (clk_i)
    variable above_now   : std_logic;
    variable cfg_changed : boolean;
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        above       <= '0';
        trigger_o   <= '0';
        threshold_q <= threshold_i;
        polarity_q  <= polarity_i;
      else
        if signed(adc_data_i) >= signed(threshold_i) then
          above_now := '1';
        else
          above_now := '0';
        end if;

        cfg_changed := (threshold_i /= threshold_q) or (polarity_i /= polarity_q);

        if armed_i = '1' and not cfg_changed and
           ((polarity_i = '1' and above_now = '1' and above = '0') or
            (polarity_i = '0' and above_now = '0' and above = '1')) then
          trigger_o <= '1';
        else
          trigger_o <= '0';
        end if;

        above       <= above_now;
        threshold_q <= threshold_i;
        polarity_q  <= polarity_i;
      end if;
    end if;
  end process;

end architecture rtl;
