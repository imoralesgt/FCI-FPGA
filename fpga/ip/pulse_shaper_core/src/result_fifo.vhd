-- Result FIFO: decouples the per-event result rate from how promptly MicroBlaze services it.
-- Identical in structure and reasoning to psd_core/src/result_fifo.vhd (independently packaged IP
-- cores don't share a library -- see pulse_shaper_core_pkg.vhd -- so this is a private copy, not a
-- shared file); REC_WIDTH here is 96 bits (64-bit timestamp + 32-bit amplitude) rather than
-- psd_core's 160, since this core buffers one amplitude field instead of three.
--
-- Overflow policy: drop the newest result and latch a sticky flag. The stream side must NEVER be
-- stalled (trapezoidal_filter/pulse_shaper_core_top never backpressure the lockstep broadcaster),
-- so backpressuring on a full FIFO is not available; losing a result while recording that it
-- happened is the only honest option left.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.pulse_shaper_core_pkg.all;

entity result_fifo is
  generic (
    REC_WIDTH : integer := 96;
    DEPTH     : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    push_i : in std_logic;
    data_i : in std_logic_vector(REC_WIDTH - 1 downto 0);

    pop_i  : in std_logic;
    data_o : out std_logic_vector(REC_WIDTH - 1 downto 0);

    empty_o    : out std_logic;
    full_o     : out std_logic;
    level_o    : out std_logic_vector(clog2(DEPTH) downto 0);
    overflow_o : out std_logic; -- sticky; cleared by clear_i
    clear_i    : in  std_logic
  );
end entity result_fifo;

architecture rtl of result_fifo is

  constant PTR_WIDTH : integer := clog2(DEPTH);

  type mem_t is array (0 to DEPTH - 1) of std_logic_vector(REC_WIDTH - 1 downto 0);
  signal mem : mem_t;

  signal wr_ptr : unsigned(PTR_WIDTH - 1 downto 0);
  signal rd_ptr : unsigned(PTR_WIDTH - 1 downto 0);
  signal level  : unsigned(PTR_WIDTH downto 0);

  signal overflow : std_logic;

begin

  empty_o    <= '1' when level = 0 else '0';
  full_o     <= '1' when level = DEPTH else '0';
  level_o    <= std_logic_vector(level);
  overflow_o <= overflow;

  -- Asynchronous read of the head entry: the AXI4-Lite register file presents this continuously,
  -- so a read of the result registers never costs an extra handshake cycle.
  data_o <= mem(to_integer(rd_ptr));

  process (clk_i)
    variable do_push : boolean;
    variable do_pop  : boolean;
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        wr_ptr   <= (others => '0');
        rd_ptr   <= (others => '0');
        level    <= (others => '0');
        overflow <= '0';
      elsif clear_i = '1' then
        wr_ptr   <= (others => '0');
        rd_ptr   <= (others => '0');
        level    <= (others => '0');
        overflow <= '0';
      else
        do_push := (push_i = '1') and (level < DEPTH);
        do_pop  := (pop_i = '1') and (level > 0);

        if push_i = '1' and level = DEPTH then
          overflow <= '1';
        end if;

        if do_push then
          mem(to_integer(wr_ptr)) <= data_i;
          if wr_ptr = DEPTH - 1 then
            wr_ptr <= (others => '0');
          else
            wr_ptr <= wr_ptr + 1;
          end if;
        end if;

        if do_pop then
          if rd_ptr = DEPTH - 1 then
            rd_ptr <= (others => '0');
          else
            rd_ptr <= rd_ptr + 1;
          end if;
        end if;

        if do_push and not do_pop then
          level <= level + 1;
        elsif do_pop and not do_push then
          level <= level - 1;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
