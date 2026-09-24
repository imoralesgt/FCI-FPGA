-- Variable-length delay line, 0..MAX_DELAY samples: data_o is data_i from delay_sel_i cycles ago
-- (0 = pure combinational pass-through). BRAM-backed circular buffer, not the behavioral
-- shift-register array trigger_core/src/delay_line.vhd uses: two revisions of this file tried
-- matching that component's own pattern (hoping Vivado would infer SRL16E/SRLC32E from it, the
-- way delay_line.vhd's own header comment claims), and BOTH measured with ZERO entries in
-- Vivado's own synthesis-log Shift-Register inference reporting -- every bit of the array became
-- a plain flip-flop plus a fabric-logic 128:1 read mux instead, ~5000 LUTs for three instances at
-- MAX_DELAY=128. Root cause not pinned down; rather than a third guess at the SRL idiom, this
-- sidesteps the question: a single write port + single independent read port at one address each
-- per cycle is the textbook simple-dual-port block RAM pattern, which Vivado infers far more
-- reliably than a MUX-selected SRL chain, and this device has BRAM headroom (64% used, RAMB18
-- specifically only 10%) where it has essentially none left in Slice LUTs.
--
-- Addressing. write_ptr advances by one every enabled cycle; data_i lands at mem(write_ptr) and
-- is visible starting the NEXT cycle (standard synchronous write). A block RAM read is ALSO
-- registered -- data presented at read_addr this cycle appears on the output one cycle later, not
-- the same cycle -- so an N-cycle delay needs read_addr = write_ptr - (N-1), not write_ptr - N, to
-- come out exactly N cycles behind data_i once that extra pipe stage is accounted for. That
-- formula only holds for N>=2: for N=1 it would ask to read back data_i from the SAME cycle it is
-- being written, which a plain simple-dual-port RAM (independent read/write addresses, no
-- same-cycle write-first behavior) cannot do -- handled instead with a one-cycle bypass register,
-- exactly like delay_val=0 is handled by bypassing the memory entirely.
--
-- Free-running, still. Genuinely never cleared except at power-on (rstn_i), matching the previous
-- (shift-register) revision's own conclusion: clearing a wide array cost the same as not clearing
-- it (confirmed by direct A/B synthesis measurement, see the project log), and clearing an entire
-- BRAM synchronously in one cycle is not even straightforwardly possible the way clearing a
-- register array is. A tap can therefore still read a sample left over from the previous
-- triggered event or the idle gap before this one, for a window bounded by this instance's own
-- MAX_DELAY -- accepted, not guarded against, per the same reasoning as before.
--
-- MAX_DELAY is a delay CEILING, not the buffer size: read_addr's subtraction wraps at 2**ADDR_WIDTH
-- via plain unsigned arithmetic, so the array has to be exactly that big or the wrap addresses
-- outside it. The array is therefore sized 2**ADDR_WIDTH (>= MAX_DELAY always, since
-- 2**clog2(MAX_DELAY-1) > MAX_DELAY-1), and MAX_DELAY itself is free to be any value.
--
-- An earlier revision instead REQUIRED MAX_DELAY to be an exact power of 2 and asserted it. That
-- assertion is worthless in this flow: Vivado synthesis does not evaluate VHDL asserts, so
-- pulse_shaper_core_0's K_MAX/M_MAX=250 silently built a 250-entry array behind an 8-bit pointer
-- that wraps at 256, leaving six addresses per lap outside the array's declared bounds -- a
-- simulation bounds error, and in hardware a silent dependency on how the inferred BRAM happens
-- to handle the overhang. Rounding the array up removes the constraint rather than restating a
-- rule nothing enforces. Costs nothing when MAX_DELAY already is a power of 2 (the two are then
-- equal), and block RAM is allocated in powers of 2 regardless.
--
-- 0 is a valid, distinct delay (delay_line.vhd's own clamp floors at 2) -- pulse_shaper_core's
-- flat_top parameter is specified 0..256 and a flat_top of 0 is a legitimate "triangular, no flat
-- top" configuration, not an error to silently round up from.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.pulse_shaper_core_pkg.all;

entity variable_delay is
  generic (
    DATA_WIDTH : integer := 16;
    MAX_DELAY  : integer := 128
  );
  port (
    clk_i       : in  std_logic;
    rstn_i      : in  std_logic;
    en_i        : in  std_logic; -- shift/write enable; holds when low (an idle inter-frame gap)
    delay_sel_i : in  std_logic_vector(clog2(MAX_DELAY) - 1 downto 0); -- valid range: 0..MAX_DELAY
    data_i      : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    data_o      : out std_logic_vector(DATA_WIDTH - 1 downto 0)
  );
end entity variable_delay;

architecture rtl of variable_delay is

  -- clog2(MAX_DELAY) (used for delay_sel_i's own port width above) is "bits to represent the
  -- VALUE MAX_DELAY itself" -- one more than needed here, where MAX_DELAY is the array's SIZE and
  -- valid indices only run 0..MAX_DELAY-1. clog2(MAX_DELAY-1) is the right count for that.
  constant ADDR_WIDTH : integer := clog2(MAX_DELAY - 1);

  -- The array's real size, which is what write_ptr's wraparound is bounded by -- not MAX_DELAY,
  -- which only bounds the DELAY a caller may request. Equal to MAX_DELAY whenever that is a power
  -- of 2, and the next power of 2 up otherwise; never smaller, so every clamped delay_val still
  -- addresses a real entry. See the header for why this is rounded rather than constrained.
  constant MEM_DEPTH : integer := 2 ** ADDR_WIDTH;

  type mem_t is array (0 to MEM_DEPTH - 1) of std_logic_vector(DATA_WIDTH - 1 downto 0);
  -- Explicit all-zero initial value, not left to a hardware/simulator default: before write_ptr
  -- has completed one full lap, a small delay_val's read_addr can still reference an address
  -- nothing has written yet, and that must read as "no history existed before this frame" (zero),
  -- not whatever a block RAM happens to power up holding. This initial value is also what
  -- synthesizes into the inferred BRAM's own INIT contents, so the guarantee holds in real
  -- hardware the same way it does here, not just in simulation.
  signal mem : mem_t := (others => (others => '0'));

  signal write_ptr : unsigned(ADDR_WIDTH - 1 downto 0);
  signal bram_dout : std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal bypass1   : std_logic_vector(DATA_WIDTH - 1 downto 0); -- 1-cycle register, delay_val=1

  -- Upper-bound clamp only: 0 is handled as a separate mux leg below (there is no "tap -1" to
  -- address), so nothing here needs a lower floor the way delay_line.vhd's clamp_delay does.
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

  -- Structural invariant, not a constraint on the caller: MEM_DEPTH is derived so this always
  -- holds. Kept as a simulation tripwire in case ADDR_WIDTH's derivation is ever changed without
  -- the array's alongside it -- deliberately NOT relied on to catch a bad generic, since Vivado
  -- synthesis ignores asserts (see the header).
  assert (MEM_DEPTH >= MAX_DELAY)
    report "variable_delay: mem is smaller than MAX_DELAY -- a clamped delay_val would address " &
           "outside the array."
    severity failure;

  delay_val <= clamp_delay(to_integer(unsigned(delay_sel_i)));

  process (clk_i)
    variable read_addr : unsigned(ADDR_WIDTH - 1 downto 0);
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        write_ptr <= (others => '0');
        bram_dout <= (others => '0');
        bypass1   <= (others => '0');
      elsif en_i = '1' then
        if delay_val >= 2 then
          read_addr := write_ptr - to_unsigned(delay_val - 1, ADDR_WIDTH);
        else
          read_addr := write_ptr; -- unused by data_o below when delay_val < 2
        end if;

        mem(to_integer(write_ptr)) <= data_i;
        bram_dout <= mem(to_integer(read_addr));
        bypass1   <= data_i;
        write_ptr <= write_ptr + 1;
      end if;
    end if;
  end process;

  data_o <= data_i  when delay_val = 0 else
            bypass1  when delay_val = 1 else
            bram_dout;

end architecture rtl;
