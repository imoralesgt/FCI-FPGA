-- Wrappers used only by compare_trigger_area.tcl, to pin cfd_trigger's generics for the area
-- comparison. They exist because `synth_design -generic` silently does nothing for this entity --
-- DLY_MAX=8 still synthesised 16 SRLs, identical to the default 32 -- so every configuration came
-- out the same and the comparison was meaningless. An explicit generic map cannot be ignored.
library ieee;
use ieee.std_logic_1164.all;
use work.trigger_core_pkg.all;

entity cfd_area_dsp is
  port (
    clk_i, rstn_i   : in  std_logic;
    adc_data_i      : in  std_logic_vector(15 downto 0);
    arm_threshold_i : in  std_logic_vector(15 downto 0);
    delay_i         : in  std_logic_vector(clog2(31) - 1 downto 0);
    frac_i          : in  std_logic_vector(7 downto 0);
    polarity_i      : in  std_logic;
    armed_i         : in  std_logic;
    trigger_o       : out std_logic
  );
end entity cfd_area_dsp;

architecture w of cfd_area_dsp is
begin
  u : entity work.cfd_trigger
    generic map (DATA_WIDTH => 16, DLY_MAX => 32, FRAC_WIDTH => 8,
                 ARM_WINDOW => 64, USE_DSP => true)
    port map (clk_i => clk_i, rstn_i => rstn_i, adc_data_i => adc_data_i,
              arm_threshold_i => arm_threshold_i, delay_i => delay_i, frac_i => frac_i,
              polarity_i => polarity_i, armed_i => armed_i, trigger_o => trigger_o);
end architecture w;

-- Fixed 1/2 fraction: no multiplier at all.
library ieee;
use ieee.std_logic_1164.all;
use work.trigger_core_pkg.all;

entity cfd_area_shift is
  port (
    clk_i, rstn_i   : in  std_logic;
    adc_data_i      : in  std_logic_vector(15 downto 0);
    arm_threshold_i : in  std_logic_vector(15 downto 0);
    delay_i         : in  std_logic_vector(clog2(31) - 1 downto 0);
    frac_i          : in  std_logic_vector(7 downto 0);
    polarity_i      : in  std_logic;
    armed_i         : in  std_logic;
    trigger_o       : out std_logic
  );
end entity cfd_area_shift;

architecture w of cfd_area_shift is
begin
  u : entity work.cfd_trigger
    generic map (DATA_WIDTH => 16, DLY_MAX => 32, FRAC_WIDTH => 8,
                 ARM_WINDOW => 64, USE_DSP => false)
    port map (clk_i => clk_i, rstn_i => rstn_i, adc_data_i => adc_data_i,
              arm_threshold_i => arm_threshold_i, delay_i => delay_i, frac_i => frac_i,
              polarity_i => polarity_i, armed_i => armed_i, trigger_o => trigger_o);
end architecture w;

-- Short delay line: a CFD delay only needs to reach a useful point on the leading edge.
library ieee;
use ieee.std_logic_1164.all;
use work.trigger_core_pkg.all;

entity cfd_area_short is
  port (
    clk_i, rstn_i   : in  std_logic;
    adc_data_i      : in  std_logic_vector(15 downto 0);
    arm_threshold_i : in  std_logic_vector(15 downto 0);
    delay_i         : in  std_logic_vector(clog2(7) - 1 downto 0);
    frac_i          : in  std_logic_vector(7 downto 0);
    polarity_i      : in  std_logic;
    armed_i         : in  std_logic;
    trigger_o       : out std_logic
  );
end entity cfd_area_short;

architecture w of cfd_area_short is
begin
  u : entity work.cfd_trigger
    generic map (DATA_WIDTH => 16, DLY_MAX => 8, FRAC_WIDTH => 8,
                 ARM_WINDOW => 64, USE_DSP => true)
    port map (clk_i => clk_i, rstn_i => rstn_i, adc_data_i => adc_data_i,
              arm_threshold_i => arm_threshold_i, delay_i => delay_i, frac_i => frac_i,
              polarity_i => polarity_i, armed_i => armed_i, trigger_o => trigger_o);
end architecture w;
