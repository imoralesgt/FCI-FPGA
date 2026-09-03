-- Result FIFO: gives the CPU an ATOMIC multi-word view of one event's result.
--
-- The rate argument for buffering is real but secondary -- at 15 kcps a six-word read plus ISR
-- overhead is roughly 400 of the 3333 cycles available per event. The argument that actually
-- forces this design is atomicity, and it applies just as much at 30 cps.
--
-- A result here is four 32-bit words (psa_l, psa_w, and a 64-bit timestamp), but AXI4-Lite reads
-- are 32 bits at a time. If the hardware could update those registers in place, a read sequence
-- could straddle an update and return psa_l from one event beside a timestamp from another -- a
-- torn record that looks entirely plausible and is silently wrong. A FIFO with an EXPLICIT pop has
-- no update-in-place at all: pushes land at wr_ptr, the CPU reads the head at rd_ptr, and rd_ptr
-- moves only when the CPU writes the pop strobe. The head is frozen for as many reads as the CPU
-- cares to make, so consistency is structural rather than something firmware has to police with a
-- seqlock and a retry loop.
--
-- The firmware contract that follows: read every field, THEN pop. Never pop between fields. And
-- pop is a write side-effect rather than a read side-effect, so reads stay idempotent and safe to
-- repeat under a debugger.
--
-- Why this exists rather than a plain result register pair. At the 30 cps this detector currently
-- sees, a register pair would be ample -- 33 ms per event against a sub-microsecond read. The
-- design target is 15 kcps, where the budget falls to 66.7 us (3333 cycles at 50 MHz). A six-word
-- read plus MicroBlaze ISR entry/exit is roughly 400 cycles, so the CPU keeps up comfortably on
-- average; what it cannot guarantee is servicing EVERY event before the next one lands. One late
-- interrupt and a register pair has silently lost an event with nothing to show for it.
--
-- A 32-deep FIFO turns that 66.7 us deadline into 2.1 ms of slack and lets the CPU drain in
-- batches on a watermark rather than taking 15,000 interrupts a second. It costs roughly 130 LUTs
-- of distributed RAM, against ~1200 LUTs and 2 BRAM tiles for the AXI DMA channel that would be
-- the alternative -- which matters on a device already at 81.6% LUT occupancy.
--
-- Overflow policy: drop the newest result and latch a sticky flag. The stream side must NEVER be
-- stalled (see fci_core_rtl_top's header), so backpressuring on a full FIFO is not available; losing
-- a result while recording that it happened is the only honest option left.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.fci_core_pkg.all;

entity result_fifo is
  generic (
    REC_WIDTH : integer := 128;
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
