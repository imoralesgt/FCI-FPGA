-- Variable-length delay line, 2..MAX_DELAY samples, used to provide pre-trigger lookback:
-- data_o at any given cycle is data_i from delay_sel_i cycles ago. Runs continuously every
-- cycle regardless of any trigger condition -- that continuous operation is what gives the
-- downstream capture logic its pre-trigger history.
--
-- Behavioral shift-register description; Vivado synthesis infers SRL16E/SRLC32E primitives
-- from this pattern automatically (no need to hand-instantiate them).
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity delay_line is
  generic (
    DATA_WIDTH : integer := 14;
    MAX_DELAY  : integer := 256
  );
  port (
    clk_i       : in  std_logic;
    rstn_i      : in  std_logic;
    delay_sel_i : in  std_logic_vector(clog2(MAX_DELAY) - 1 downto 0); -- valid range: 2..MAX_DELAY
    data_i      : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    data_o      : out std_logic_vector(DATA_WIDTH - 1 downto 0)
  );
end entity delay_line;

architecture rtl of delay_line is

  type shift_array_t is array (0 to MAX_DELAY - 1) of std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal shift_reg : shift_array_t;

  -- Clamp to the documented valid range [2, MAX_DELAY] so a misprogrammed register can't index
  -- outside the array.
  function clamp_delay(v : natural) return natural is
  begin
    if v < 2 then
      return 2;
    elsif v > MAX_DELAY then
      return MAX_DELAY;
    else
      return v;
    end if;
  end function clamp_delay;

begin

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        shift_reg <= (others => (others => '0'));
      else
        shift_reg(0) <= data_i;
        for i in 1 to MAX_DELAY - 1 loop
          shift_reg(i) <= shift_reg(i - 1);
        end loop;
      end if;
    end if;
  end process;

  -- shift_reg(k) is (k+1)-cycle delayed data_i, so an N-cycle delay reads tap index N-1.
  data_o <= shift_reg(clamp_delay(to_integer(unsigned(delay_sel_i))) - 1);

end architecture rtl;
