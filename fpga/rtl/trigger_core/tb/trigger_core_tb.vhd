-- Self-checking testbench for trigger_core_top.
--
-- Stimulus strategy: drive a ramp where adc_data_i directly encodes its own sample index
-- (mod 16384). This makes verification robust to the exact internal pipeline latency (which
-- cycle, precisely, capture starts on relative to the live threshold crossing) -- instead of
-- hand-deriving that offset, the captured/streamed *values* themselves reveal which original
-- samples got captured, so correctness is checked by:
--   1. exactly `depth` beats are produced, tlast on exactly the last one;
--   2. the streamed-out values are `depth` consecutive indices (ascending or descending to match
--      stimulus direction) -- proves no sample was dropped, duplicated, or reordered;
--   3. the known threshold-crossing index falls within the captured window at the expected
--      offset from its start (proves the delay-line pre-trigger split itself is correct, not
--      just "some contiguous run was captured").
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity trigger_core_tb is
end entity trigger_core_tb;

architecture sim of trigger_core_tb is

  constant ADC_WIDTH  : integer := 14;
  constant MAX_DELAY  : integer := 256;
  constant MAX_DEPTH  : integer := 4096;
  constant CLK_PERIOD : time    := 20 ns;
  constant MOD_VAL     : integer := 16384; -- 2**ADC_WIDTH

  signal clk_i      : std_logic := '0';
  signal rstn_i     : std_logic := '0';
  signal adc_data_i : std_logic_vector(ADC_WIDTH - 1 downto 0) := (others => '0');

  signal s_axi_awaddr  : std_logic_vector(4 downto 0)  := (others => '0');
  signal s_axi_awvalid : std_logic := '0';
  signal s_axi_awready : std_logic;
  signal s_axi_wdata   : std_logic_vector(31 downto 0) := (others => '0');
  signal s_axi_wstrb   : std_logic_vector(3 downto 0)  := (others => '0');
  signal s_axi_wvalid  : std_logic := '0';
  signal s_axi_wready  : std_logic;
  signal s_axi_bresp   : std_logic_vector(1 downto 0);
  signal s_axi_bvalid  : std_logic;
  signal s_axi_bready  : std_logic := '0';
  signal s_axi_araddr  : std_logic_vector(4 downto 0)  := (others => '0');
  signal s_axi_arvalid : std_logic := '0';
  signal s_axi_arready : std_logic;
  signal s_axi_rdata   : std_logic_vector(31 downto 0);
  signal s_axi_rresp   : std_logic_vector(1 downto 0);
  signal s_axi_rvalid  : std_logic;
  signal s_axi_rready  : std_logic := '0';

  signal m_axis_tdata  : std_logic_vector(15 downto 0);
  signal m_axis_tkeep  : std_logic_vector(1 downto 0);
  signal m_axis_tstrb  : std_logic_vector(1 downto 0);
  signal m_axis_tuser  : std_logic_vector(0 downto 0);
  signal m_axis_tlast  : std_logic;
  signal m_axis_tid    : std_logic_vector(0 downto 0);
  signal m_axis_tdest  : std_logic_vector(0 downto 0);
  signal m_axis_tvalid : std_logic;
  -- Default '1' (not '0'): reconfiguring threshold/polarity live can itself momentarily look
  -- like a crossing to the comparator (e.g. threshold changing while adc_data_i is still at
  -- reset's default) before delay/depth have their new values too -- a real, if narrow, hazard
  -- of reconfiguring a live comparator, not simulator-only. run_test briefly asserts tready
  -- right after its config writes to drain any such spurious trace, then drops it back to '0'
  -- before driving the real stimulus -- capture_engine correctly stalls (holding its output)
  -- until tready is asserted again in the explicit consume phase below, so the real capture is
  -- never lost regardless of exactly when its trigger fires relative to the stimulus loop.
  signal m_axis_tready : std_logic := '0';

  signal test_count : integer := 0;
  signal fail_count : integer := 0;

  -- Set true only around the reconfiguration-hazard test below, while adc_data_i is held
  -- deliberately constant -- any m_axis_tvalid rising edge seen during that window can only be a
  -- spurious, reconfiguration-induced capture (trigger.vhd's now-fixed bug), not a real one.
  signal expect_no_capture : boolean := false;
  signal reconfig_fail     : boolean := false;

  -- Stimulus helper: encodes an intended offset-binary sample value as the raw 2's-complement
  -- bit pattern a real LTC2248 (MODE strapped to 2/3 VDD on this board) actually outputs for it
  -- -- mirrors trigger_core_top's own (self-inverse, MSB-flip) conversion. Driving adc_data_i
  -- through this function means every assertion below, expressed in offset-binary terms exactly
  -- as before this conversion existed, continues to hold unchanged if and only if that
  -- conversion is implemented correctly.
  function ob_to_2c(v : integer) return std_logic_vector is
    variable result : std_logic_vector(ADC_WIDTH - 1 downto 0);
  begin
    result := std_logic_vector(to_unsigned(v mod MOD_VAL, ADC_WIDTH));
    result(ADC_WIDTH - 1) := not result(ADC_WIDTH - 1);
    return result;
  end function;

begin

  clk_i <= not clk_i after CLK_PERIOD / 2;

  -- Sole driver of reconfig_fail (a single un-resolved boolean can't have two drivers): re-arms
  -- itself to false on expect_no_capture's rising edge, then latches true on any spurious
  -- capture seen while it's set -- the stimulus process only ever reads it.
  monitor_no_spurious : process
    variable prev_tvalid : std_logic := '0';
    variable prev_expect : boolean   := false;
  begin
    wait until rising_edge(clk_i);
    if expect_no_capture and not prev_expect then
      reconfig_fail <= false;
    end if;
    if expect_no_capture and m_axis_tvalid = '1' and prev_tvalid = '0' then
      report "  FAIL: spurious capture (m_axis_tvalid rose) during reconfiguration-only stimulus"
        severity error;
      reconfig_fail <= true;
    end if;
    prev_tvalid := m_axis_tvalid;
    prev_expect := expect_no_capture;
  end process;

  dut : entity work.trigger_core_top
    generic map (
      ADC_WIDTH => ADC_WIDTH,
      MAX_DELAY => MAX_DELAY,
      MAX_DEPTH => MAX_DEPTH
    )
    port map (
      clk_i         => clk_i,
      rstn_i        => rstn_i,
      adc_data_i    => adc_data_i,
      s_axi_awaddr  => s_axi_awaddr,
      s_axi_awvalid => s_axi_awvalid,
      s_axi_awready => s_axi_awready,
      s_axi_wdata   => s_axi_wdata,
      s_axi_wstrb   => s_axi_wstrb,
      s_axi_wvalid  => s_axi_wvalid,
      s_axi_wready  => s_axi_wready,
      s_axi_bresp   => s_axi_bresp,
      s_axi_bvalid  => s_axi_bvalid,
      s_axi_bready  => s_axi_bready,
      s_axi_araddr  => s_axi_araddr,
      s_axi_arvalid => s_axi_arvalid,
      s_axi_arready => s_axi_arready,
      s_axi_rdata   => s_axi_rdata,
      s_axi_rresp   => s_axi_rresp,
      s_axi_rvalid  => s_axi_rvalid,
      s_axi_rready  => s_axi_rready,
      m_axis_tdata  => m_axis_tdata,
      m_axis_tkeep  => m_axis_tkeep,
      m_axis_tstrb  => m_axis_tstrb,
      m_axis_tuser  => m_axis_tuser,
      m_axis_tlast  => m_axis_tlast,
      m_axis_tid    => m_axis_tid,
      m_axis_tdest  => m_axis_tdest,
      m_axis_tvalid => m_axis_tvalid,
      m_axis_tready => m_axis_tready
    );

  stimulus : process

    procedure axi_write(addr : natural; data : natural) is
    begin
      wait until rising_edge(clk_i);
      s_axi_awaddr  <= std_logic_vector(to_unsigned(addr, 5));
      s_axi_awvalid <= '1';
      s_axi_wdata   <= std_logic_vector(to_unsigned(data, 32));
      s_axi_wstrb   <= "1111";
      s_axi_wvalid  <= '1';
      wait until rising_edge(clk_i) and s_axi_awready = '1' and s_axi_wready = '1';
      s_axi_awvalid <= '0';
      s_axi_wvalid  <= '0';
      s_axi_bready  <= '1';
      wait until rising_edge(clk_i) and s_axi_bvalid = '1';
      s_axi_bready <= '0';
    end procedure;

    procedure run_test(
      test_name     : string;
      threshold     : natural;
      polarity      : std_logic;
      delay_v       : natural;
      depth_v       : natural;
      ascending     : boolean;
      backpressure  : boolean := false;
      -- Long-stall mode: hold tready low for hundreds of cycles at a time, the way fci_core does
      -- while it works through a frame (its interval is 3197 cycles, so trigger_core sees a stall
      -- far longer than the short 1-in-7 hiccup `backpressure` models).
      long_stall    : boolean := false
    ) is
      variable crossing_offset : integer;
      variable beats           : integer := 0;
      variable first_val       : integer := -1;
      variable prev_val        : integer := -1;
      variable this_val        : integer;
      variable ok              : boolean := true;
      variable cross_pos       : integer := -1;
      variable cycle_count     : integer := 0;
      variable expect_pos      : integer;
      -- Throughput regression (repo issue #10): with tready held high, capture_engine must
      -- present a beat EVERY cycle once streaming has started. Any idle cycle mid-stream means
      -- the read pipeline is stalling on itself again and the effective output rate has halved.
      variable bubbles         : integer := 0;
    begin
      test_count <= test_count + 1;
      report "=== Test: " & test_name & " ===";

      axi_write(0, threshold);
      if polarity = '1' then
        axi_write(4, 1);
      else
        axi_write(4, 0);
      end if;
      axi_write(8, delay_v);
      axi_write(12, depth_v);

      -- Writing threshold/polarity live can itself look like a momentary crossing to the
      -- comparator before delay/depth catch up (e.g. reset defaults vs. the new values), firing
      -- a spurious capture. Briefly drain it here (bounded window, comfortably more than the
      -- few cycles such a capture takes) then drop tready back to '0' before driving the real
      -- stimulus -- capture_engine correctly stalls holding its output when tready is low, so
      -- the real capture (whenever its trigger fires relative to the ramp below) is safely held
      -- until the explicit consume phase picks it up, rather than needing tready held high
      -- (and possibly silently draining the *real* capture too, before this procedure is even
      -- watching for it).
      m_axis_tready <= '1';
      for i in 0 to 40 loop
        wait until rising_edge(clk_i);
      end loop;
      m_axis_tready <= '0';

      -- Ramp stimulus: value at step i is (threshold -/+ crossing_offset +/- i), so the live
      -- crossing happens exactly at step `crossing_offset`. crossing_offset is always well
      -- past delay_v so the delay line has a full, valid pre-trigger history by then.
      crossing_offset := delay_v + 20;
      for i in 0 to (crossing_offset + depth_v + 64) loop
        wait until rising_edge(clk_i);
        if ascending then
          adc_data_i <= ob_to_2c(threshold - crossing_offset + i);
        else
          adc_data_i <= ob_to_2c(threshold + crossing_offset - i);
        end if;
      end loop;

      -- Consume the AXI4-Stream output.
      beats := 0;
      loop
        cycle_count := cycle_count + 1;
        if backpressure and (cycle_count mod 7 = 0) then
          m_axis_tready <= '0';
        elsif long_stall and ((cycle_count mod 600) < 300) then
          m_axis_tready <= '0';
        else
          m_axis_tready <= '1';
        end if;
        wait until rising_edge(clk_i);
        -- Count idle cycles between the first accepted beat and tlast. Only meaningful when we
        -- are not deliberately deasserting tready ourselves.
        if (not backpressure) and (not long_stall) and beats > 0 and m_axis_tvalid = '0' then
          bubbles := bubbles + 1;
        end if;
        if m_axis_tvalid = '1' and m_axis_tready = '1' then
          this_val := to_integer(unsigned(m_axis_tdata(ADC_WIDTH - 1 downto 0)));
          if beats = 0 then
            first_val := this_val;
          else
            if ascending then
              if this_val /= (prev_val + 1) mod MOD_VAL then
                ok := false;
                report "  FAIL: beat " & integer'image(beats) & " not consecutive ascending (prev="
                       & integer'image(prev_val) & " this=" & integer'image(this_val) & ")";
              end if;
            else
              if this_val /= (prev_val - 1 + MOD_VAL) mod MOD_VAL then
                ok := false;
                report "  FAIL: beat " & integer'image(beats) & " not consecutive descending (prev="
                       & integer'image(prev_val) & " this=" & integer'image(this_val) & ")";
              end if;
            end if;
          end if;
          if this_val = threshold then
            cross_pos := beats;
          end if;
          prev_val := this_val;
          beats    := beats + 1;
          if m_axis_tlast = '1' then
            if beats /= depth_v then
              ok := false;
              report "  FAIL: tlast on beat " & integer'image(beats) & ", expected " & integer'image(depth_v);
            end if;
            exit;
          end if;
        end if;
      end loop;
      m_axis_tready <= '1'; -- back to default drain-anything state between tests

      if beats /= depth_v then
        ok := false;
        report "  FAIL: expected " & integer'image(depth_v) & " beats, got " & integer'image(beats);
      end if;

      if (not backpressure) and (not long_stall) and bubbles /= 0 then
        ok := false;
        report "  FAIL: " & integer'image(bubbles) & " idle cycle(s) mid-stream with tready held "
               & "high -- output rate is not one beat per cycle (issue #10)";
      end if;

      -- The crossing sample should land at position `delay_v` from the start of the capture
      -- (delay_v samples of pre-trigger history, then the crossing sample itself, then
      -- post-trigger samples) -- allow a small fixed tolerance for the trigger/capture pipeline's
      -- own registered stages rather than assuming an exact hand-derived constant.
      if cross_pos = -1 then
        ok := false;
        report "  FAIL: crossing value " & integer'image(threshold) & " never appeared in capture";
      else
        expect_pos := delay_v;
        if abs(cross_pos - expect_pos) > 3 then
          ok := false;
          report "  FAIL: crossing at position " & integer'image(cross_pos) & ", expected near "
                 & integer'image(expect_pos) & " (delay_v)";
        end if;
      end if;

      if ok then
        report "  PASS (first_val=" & integer'image(first_val) & ", beats=" & integer'image(beats)
               & ", cross_pos=" & integer'image(cross_pos) & ", mid-stream idle cycles="
               & integer'image(bubbles) & ")";
      else
        fail_count <= fail_count + 1;
        report "  Test '" & test_name & "' FAILED" severity error;
      end if;

      for i in 0 to 3 loop
        wait until rising_edge(clk_i);
      end loop;
    end procedure;

  begin
    rstn_i <= '0';
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    rstn_i <= '1';
    wait until rising_edge(clk_i);

    run_test("negative-going small", 90, '0', 4, 16, false);
    run_test("positive-going small", 150, '1', 8, 32, true);
    run_test("positive-going with backpressure", 150, '1', 8, 32, true, true);
    run_test("boundary delay=2", 150, '1', 2, 16, true);
    run_test("boundary delay=256", 356, '1', 256, 300, true);
    run_test("boundary depth=4096", 150, '1', 4, 4096, true);
    -- Mirrors the real system: depth 1024 with stalls the length of an fci_core frame.
    run_test("long stall (fci_core-like), depth=1024", 150, '1', 100, 1024, true, false, true);

    -- Reconfiguration hazard: on real hardware, reprogramming threshold/polarity while
    -- adc_data_i doesn't move produced spurious "triggered" captures full of pure baseline noise
    -- -- trigger.vhd's `above` was being compared across a threshold/polarity change boundary,
    -- which can look exactly like a genuine crossing. Reproduces the same sequence here: hold
    -- adc_data_i fixed and drive threshold/polarity through transitions that would have crossed
    -- the old (leftover) comparator state, with monitor_no_spurious watching for any capture.
    report "=== Test: reconfiguration hazard (no spurious capture) ===";
    test_count    <= test_count + 1;
    m_axis_tready <= '1';

    -- Small, known depth/delay first (unrelated to the threshold/polarity hazard itself) so
    -- that if the upcoming adc_data_i jump causes one real, legitimate trigger under whatever
    -- threshold/polarity the previous test left behind, its capture drains quickly instead of
    -- running for a leftover depth of up to 4096.
    axi_write(8, 4);
    axi_write(12, 16);

    adc_data_i <= ob_to_2c(5000);
    for i in 0 to 39 loop
      wait until rising_edge(clk_i);
    end loop;

    expect_no_capture <= true;

    axi_write(0, 9000); -- threshold -> 9000: baseline (5000) < 9000, above settles to '0'
    for i in 0 to 9 loop
      wait until rising_edge(clk_i);
    end loop;

    axi_write(4, 1); -- polarity -> RISING; threshold unchanged, above unchanged -- mundane
    for i in 0 to 9 loop
      wait until rising_edge(clk_i);
    end loop;

    axi_write(0, 0); -- threshold 9000 -> 0: RISING hazard (above would jump 0->1 with adc_data_i
                      -- untouched)
    for i in 0 to 9 loop
      wait until rising_edge(clk_i);
    end loop;

    axi_write(4, 0); -- polarity -> FALLING; threshold unchanged (0), above unchanged -- mundane
    for i in 0 to 9 loop
      wait until rising_edge(clk_i);
    end loop;

    axi_write(0, 16383); -- threshold 0 -> 16383: FALLING hazard (above would jump 1->0 with
                          -- adc_data_i untouched)
    for i in 0 to 19 loop
      wait until rising_edge(clk_i);
    end loop;

    expect_no_capture <= false;

    if reconfig_fail then
      fail_count <= fail_count + 1;
      report "  Test 'reconfiguration hazard' FAILED" severity error;
    else
      report "  PASS (no spurious capture across 5 threshold/polarity reconfigurations, "
             & "adc_data_i held constant)";
    end if;

    for i in 0 to 3 loop
      wait until rising_edge(clk_i);
    end loop;

    report "=== " & integer'image(test_count) & " tests run, " & integer'image(fail_count) & " failed ===";
    if fail_count = 0 then
      report "TEST PASSED";
    else
      report "TEST FAILED" severity error;
    end if;

    std.env.stop;
  end process stimulus;

end architecture sim;
