-- Simple dual-port RAM used as the trace capture buffer: one write port (used during CAPTURE),
-- one read port (used during STREAM), independent addressing. Synchronous read (registered
-- output) so Vivado infers a block RAM rather than distributed RAM.
--
-- "Circular" here describes the address space (write/read addresses each wrap at DEPTH, driven
-- by capture_engine's counters), not a free-running overwrite-in-place ring buffer -- capture
-- and stream never happen concurrently in this design (see project plan: single-buffered).
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity circular_buffer is
  generic (
    DATA_WIDTH : integer := 14;
    DEPTH      : integer := 4096
  );
  port (
    clk_i     : in  std_logic;
    wr_en_i   : in  std_logic;
    wr_addr_i : in  std_logic_vector(clog2(DEPTH - 1) - 1 downto 0);
    wr_data_i : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    rd_en_i   : in  std_logic;
    rd_addr_i : in  std_logic_vector(clog2(DEPTH - 1) - 1 downto 0);
    rd_data_o : out std_logic_vector(DATA_WIDTH - 1 downto 0)
  );
end entity circular_buffer;

architecture rtl of circular_buffer is

  type ram_array_t is array (0 to DEPTH - 1) of std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal ram : ram_array_t := (others => (others => '0'));

begin

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if wr_en_i = '1' then
        ram(to_integer(unsigned(wr_addr_i))) <= wr_data_i;
      end if;
      if rd_en_i = '1' then
        rd_data_o <= ram(to_integer(unsigned(rd_addr_i)));
      end if;
    end if;
  end process;

end architecture rtl;
