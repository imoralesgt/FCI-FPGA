-- Self-checking testbench: a frame's shaped amplitude must not depend on the frame before it.
--
-- trapezoidal_filter's delay taps are free-running, so without per-frame masking they start each
-- frame holding the end of the previous one -- Tr' = x + Pz/M there, i.e. the previous frame's
-- integrated signal and baseline scaled by 1/decay. That leaked into every event as a random
-- offset: on recorded PMT CLYC traces it widened the 511 keV line from 9.6% to 13.3% FWHM (see
-- the filter's "Per-frame reset" header note). This test builds the worst case for it on purpose:
-- a short decay (10 samples, so 1/M is large), and a "dirty" preceding frame with a large pulse
-- AND a DC offset, which drive Pz -- and so the stale taps -- as far from zero as a real frame can.
--
-- Checks, each against the same filter output from a clean start (after reset):
--   1. an all-zero frame following the dirty frame shapes to exactly 0;
--   2. a test pulse following the dirty frame shapes to exactly the value it has after reset;
--   3. the same, with a flat_top of 0 (l = k, so two taps share one delay).
-- Equality, not a tolerance: from identical frame contents the filter is deterministic, so any
-- difference at all is state carried over from the previous frame.
--
-- valid is asserted one cycle in three, as on hardware (150 MHz core clock, 50 Msps stream), with
-- an idle gap between frames.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
use work.pulse_shaper_core_pkg.all;

entity trapezoidal_filter_frame_tb is
end entity trapezoidal_filter_frame_tb;

architecture sim of trapezoidal_filter_frame_tb is
  constant FRAME_LEN  : integer := 2048; -- frame depth, as trigger_core is configured.
  -- Not "N": VHDL is case-insensitive, and the frame loop's own index is n.
  constant PRE        : integer := 64;   -- pulse onset sample, as the trigger delay places it
  constant CLK_PERIOD : time := 6.667 ns;

  signal clk_i  : std_logic := '0';
  signal rstn_i : std_logic := '0';
  signal valid  : std_logic := '0';
  signal last   : std_logic := '0';
  signal data   : std_logic_vector(15 downto 0) := (others => '0');
  signal peaking  : std_logic_vector(clog2(256) - 1 downto 0);
  signal flat_top : std_logic_vector(clog2(256) - 1 downto 0);
  signal recip    : std_logic_vector(17 downto 0);
  signal rvalid : std_logic;
  signal amp    : std_logic_vector(31 downto 0);
  signal done   : boolean := false;

  -- Which frame shape send_frame() drives.
  type frame_kind is (ZERO_FRAME, DIRTY_FRAME, TEST_FRAME);

  function sample(kind : frame_kind; n : integer) return integer is
  begin
    case kind is
      when ZERO_FRAME =>
        return 0;
      when DIRTY_FRAME =>
        -- A large pulse with a slow tail, sitting on a +200-count baseline offset.
        if n < PRE then
          return 200;
        else
          return 200 + integer(12000.0 * exp(-real(n - PRE) / 300.0));
        end if;
      when TEST_FRAME =>
        -- A fast-plus-slow pulse, like a CLYC gamma (CVL spike, then a slower component).
        if n < PRE then
          return 0;
        else
          return integer(1500.0 * exp(-real(n - PRE) / 2.0) + 600.0 * exp(-real(n - PRE) / 50.0));
        end if;
    end case;
  end function;
begin
  clk_i <= not clk_i after CLK_PERIOD / 2 when not done else '0';

  dut : entity work.trapezoidal_filter
    port map (
      clk_i => clk_i, rstn_i => rstn_i,
      s_valid_i => valid, s_data_i => data, s_last_i => last,
      peaking_i => peaking, flat_top_i => flat_top, decay_recip_i => recip, enable_i => '1',
      result_valid_o => rvalid, amplitude_o => amp);

  stim : process
    variable result : integer;

    procedure send_frame(kind : frame_kind; variable a : out integer) is
    begin
      for n in 0 to FRAME_LEN - 1 loop
        data  <= std_logic_vector(to_signed(sample(kind, n), 16));
        valid <= '1';
        if n = FRAME_LEN - 1 then last <= '1'; else last <= '0'; end if;
        wait until rising_edge(clk_i);
        valid <= '0';
        last  <= '0';
        wait until rising_edge(clk_i);
        wait until rising_edge(clk_i);
      end loop;
      -- The result follows the frame's last beat by the pipeline latency; wait for it, then leave
      -- an idle gap before the next frame, as between triggered events.
      loop
        wait until rising_edge(clk_i);
        exit when rvalid = '1';
      end loop;
      a := to_integer(signed(amp));
      for c in 1 to 100 loop wait until rising_edge(clk_i); end loop;
    end procedure;

    procedure do_reset is
    begin
      rstn_i <= '0';
      for c in 1 to 5 loop wait until rising_edge(clk_i); end loop;
      rstn_i <= '1';
      for c in 1 to 5 loop wait until rising_edge(clk_i); end loop;
    end procedure;

    procedure check_config(k, ft : integer) is
      variable clean, after_dirty, zero_after_dirty : integer;
    begin
      peaking  <= std_logic_vector(to_unsigned(k, peaking'length));
      flat_top <= std_logic_vector(to_unsigned(ft, flat_top'length));
      recip    <= std_logic_vector(to_signed(65536 / 10, 18)); -- decay = 10 samples

      do_reset;
      send_frame(TEST_FRAME, clean);

      send_frame(DIRTY_FRAME, result);
      send_frame(ZERO_FRAME, zero_after_dirty);
      assert zero_after_dirty = 0
        report "peaking " & integer'image(k) & ", flat_top " & integer'image(ft) &
               ": all-zero frame after a dirty frame shaped to " & integer'image(zero_after_dirty) &
               ", expected 0 (previous frame leaking in)"
        severity error;

      send_frame(DIRTY_FRAME, result);
      send_frame(TEST_FRAME, after_dirty);
      assert after_dirty = clean
        report "peaking " & integer'image(k) & ", flat_top " & integer'image(ft) &
               ": test frame after a dirty frame shaped to " & integer'image(after_dirty) &
               ", after reset to " & integer'image(clean) & " (previous frame leaking in)"
        severity error;

      report "peaking " & integer'image(k) & ", flat_top " & integer'image(ft) &
             ": clean " & integer'image(clean) & ", after dirty " & integer'image(after_dirty) &
             ", zero after dirty " & integer'image(zero_after_dirty);
    end procedure;
  begin
    check_config(10, 50);  -- the PMT CLYC setting the bug was found with (0.2/1.0/0.2 us)
    check_config(50, 1);   -- the earlier deployed setting (1.00/0.02/0.20 us)
    check_config(25, 0);   -- no flat top: l = k
    report "trapezoidal_filter_frame_tb: done";
    done <= true;
    wait;
  end process;
end architecture sim;
