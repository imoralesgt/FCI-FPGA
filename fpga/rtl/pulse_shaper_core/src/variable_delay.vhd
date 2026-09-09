-- Variable-length delay line, 0..MAX_DELAY samples: data_o is data_i from delay_sel_i cycles ago
-- (0 = pure combinational pass-through). Behavioral full-array shift register (Vivado infers
-- SRL16E/SRLC32E from this pattern automatically, same as trigger_core/src/delay_line.vhd, which
-- this is adapted from) with two differences from that component, both required by how this core
-- uses it rather than how trigger_core does:
--
--   * 0 is a valid, distinct delay (delay_line.vhd's own clamp floors at 2) -- pulse_shaper_core's
--     flat_top parameter is specified 0..250 and a flat_top of 0 is a legitimate "triangular, no
--     flat top" configuration, not an error to silently round up from.
--   * Shifting is GATED by en_i, and the whole array can be SYNCHRONOUSLY CLEARED by clear_i.
--     trigger_core's delay_line free-runs every cycle because it is fed directly by a continuous
--     50 Msps ADC stream with no idle gaps to worry about. This core instead sits downstream of
--     trigger_core's own triggered-capture output, which is a BURSTY stream -- exactly `depth`
--     beats per event with idle gaps between events (see psd_core_top.vhd's header comment on the
--     same broadcaster tap). A free-running delay line would carry the PREVIOUS event's tail
--     samples into the next frame's pipeline-fill window, corrupting the trapezoidal filter's
--     result for however many samples it takes to flush -- not a negligible edge effect, since the
--     configurable peaking+flat_top depth (up to 500 samples) can exceed the typical ~100-sample
--     pre-trigger margin and reach into where the real pulse's rising edge occurs. clear_i is
--     pulsed by trapezoidal_filter.vhd on s_last_i, so every new frame starts every tap at a known
--     zero rather than stale content from whatever the previous frame happened to end on.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.pulse_shaper_core_pkg.all;

entity variable_delay is
  generic (
    DATA_WIDTH : integer := 16;
    MAX_DELAY  : integer := 250
  );
  port (
    clk_i       : in  std_logic;
    rstn_i      : in  std_logic;
    clear_i     : in  std_logic; -- synchronous clear, same effect as rstn_i='0' but per-frame
    en_i        : in  std_logic; -- shift enable; holds all taps when low (an idle inter-frame gap)
    delay_sel_i : in  std_logic_vector(clog2(MAX_DELAY) - 1 downto 0); -- valid range: 0..MAX_DELAY
    data_i      : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    data_o      : out std_logic_vector(DATA_WIDTH - 1 downto 0)
  );
end entity variable_delay;

architecture rtl of variable_delay is

  type shift_array_t is array (0 to MAX_DELAY - 1) of std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal shift_reg : shift_array_t;

  -- Upper-bound clamp only: 0 is handled as a separate mux leg below (there is no shift_reg(-1)
  -- tap to index), so nothing here needs a lower floor the way delay_line.vhd's clamp_delay does.
  function clamp_delay(v : natural) return natural is
  begin
    if v > MAX_DELAY then
      return MAX_DELAY;
    else
      return v;
    end if;
  end function clamp_delay;

  signal delay_val : natural range 0 to MAX_DELAY;

begin

  delay_val <= clamp_delay(to_integer(unsigned(delay_sel_i)));

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' or clear_i = '1' then
        shift_reg <= (others => (others => '0'));
      elsif en_i = '1' then
        shift_reg(0) <= data_i;
        for i in 1 to MAX_DELAY - 1 loop
          shift_reg(i) <= shift_reg(i - 1);
        end loop;
      end if;
    end if;
  end process;

  -- shift_reg(k) is (k+1)-cycle delayed data_i, so an N-cycle delay (N>=1) reads tap index N-1;
  -- a 0-cycle delay bypasses the array entirely.
  data_o <= data_i when delay_val = 0 else shift_reg(delay_val - 1);

end architecture rtl;
