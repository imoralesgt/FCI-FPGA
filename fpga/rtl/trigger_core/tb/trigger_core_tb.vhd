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

  -- 16-bit signed sample datapath, matching blr_core's output. The ADC itself is 14-bit; the two
  -- extra bits are headroom for blr_core's baseline subtraction.
  constant ADC_WIDTH  : integer := 16;
  constant MAX_DELAY  : integer := 256;
  constant MAX_DEPTH  : integer := 4096;
  constant CLK_PERIOD : time    := 20 ns;
  constant MOD_VAL     : integer := 65536; -- 2**ADC_WIDTH, for the consecutive-value wrap check

  -- CFD settings used by every scenario. f = 128/256 = 0.5 and D = 20 put the ramp's crossing at
  -- s = D/(1-f) = 40, comfortably above the arming thresholds below so the pulse is always armed
  -- BEFORE the crossing arrives -- the ordering the CFD requires (see cfd_trigger.vhd's header on
  -- the sensitivity constraint; get it backwards and nothing triggers at all).
  constant CFD_FRAC_V  : integer := 128;
  constant CFD_DELAY_V : integer := 20;
  constant CFD_CROSS_VAL : integer := 40;   -- D / (1 - f), for f = 1/2

  -- Samples between the mathematical zero crossing and trigger_o going high: the delay-line read
  -- is registered, cfd_q is registered, and trigger_o is registered. Measured in cfd_trigger_tb,
  -- where the crossing is analytically at n = D/(1-f) = 16 and the trigger fires at 19.
  --
  -- It matters here because the capture is anchored to trigger_o, so the crossing SAMPLE sits
  -- CFD_LATENCY earlier in the captured window than the pre-trigger delay alone would put it --
  -- and if the pre-trigger delay is smaller than this, the crossing falls outside the window
  -- entirely. That is why firmware rejects a delay below 4 (cli.c).
  constant CFD_LATENCY : integer := 3;

  signal clk_i      : std_logic := '0';
  signal rstn_i     : std_logic := '0';
  -- Kept as a 14-bit stimulus signal and widened to the core's 16-bit TDATA below, so every
  -- existing `adc_data_i <= ob_to_2c(...)` line in the scenarios stays as it was.
  signal adc_data_i    : std_logic_vector(ADC_WIDTH - 1 downto 0) := (others => '0');
  signal s_axis_tdata  : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal s_axis_tvalid : std_logic := '1';
  signal s_axis_tready : std_logic;

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
  signal m_axis_tuser  : std_logic_vector(63 downto 0);
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

  -- Stimulus helper: the stream carries signed samples now, so driving a level is a plain signed cast -- no format
  -- conversion anywhere in the chain. Kept as a function so the scenarios read unchanged.
  function ob_to_2c(v : integer) return std_logic_vector is
    variable result : std_logic_vector(ADC_WIDTH - 1 downto 0);
  begin
    result := std_logic_vector(to_signed(v, ADC_WIDTH));
    return result;
  end function;

begin

  -- The datapath is 16-bit signed end to end now, so the stimulus vector is the TDATA vector.
  s_axis_tdata <= adc_data_i;

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
      s_axis_tdata  => s_axis_tdata,
      s_axis_tvalid => s_axis_tvalid,
      s_axis_tready => s_axis_tready,
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
      threshold     : integer;
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
      variable cross_target    : integer;
      -- Throughput regression (repo issue #10): with tready held high, capture_engine must
      -- present a beat EVERY cycle once streaming has started. Any idle cycle mid-stream means
      -- the read pipeline is stalling on itself again and the effective output rate has halved.
      variable bubbles         : integer := 0;
    begin
      test_count <= test_count + 1;
      report "=== Test: " & test_name & " ===";

      -- Threshold is a SIGNED level but the register write takes the raw word, so a negative
      -- level goes out in two's complement -- exactly as firmware writes it (cli.c masks with
      -- 0xFFFF for the same reason).
      axi_write(0, (threshold + MOD_VAL) mod MOD_VAL);
      if polarity = '1' then
        axi_write(4, 1);
      else
        axi_write(4, 0);
      end if;
      axi_write(8, delay_v);
      axi_write(12, depth_v);
      -- CFD parameters (0x10 fraction, 0x14 delay). On a LINEAR RAMP the CFD reduces to a level
      -- trigger at a known value: with s[n] = a + n,
      --     cfd[n] = s[n-D] - f*s[n] = (1-f)*s[n] - D
      -- which is zero at s = D/(1-f), independent of where the ramp started. So the ramp
      -- stimulus still gives an exactly predictable trigger point -- just at CFD_CROSS_VAL
      -- rather than at the threshold, which is what this test used to key on.
      axi_write(16, CFD_FRAC_V);
      axi_write(20, CFD_DELAY_V);

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
      if ascending then
        cross_target := CFD_CROSS_VAL;
      else
        cross_target := (MOD_VAL - CFD_CROSS_VAL) mod MOD_VAL;  -- -CFD_CROSS_VAL, as unsigned
      end if;
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
          this_val := to_integer(unsigned(m_axis_tdata));
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
          -- The CFD fires at a fixed SAMPLE VALUE on a ramp (see CFD_CROSS_VAL), not at the
          -- threshold -- the threshold only arms it.
          if this_val = cross_target then
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
      expect_pos := delay_v - CFD_LATENCY;
      if expect_pos < 0 then
        -- The crossing sample precedes the captured window; nothing to assert. The capture
        -- itself is still valid and every other check above still applies.
        report "    (crossing-position check skipped: delay_v " & integer'image(delay_v)
               & " <= CFD latency " & integer'image(CFD_LATENCY) & ")";
      elsif cross_pos = -1 then
        ok := false;
        report "  FAIL: CFD crossing value " & integer'image(cross_target)
               & " never appeared in capture";
      elsif abs(cross_pos - expect_pos) > 3 then
        ok := false;
        report "  FAIL: crossing at position " & integer'image(cross_pos) & ", expected near "
               & integer'image(expect_pos) & " (delay_v - CFD latency)";
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

    -- Double-buffering scenario state (see the scenario below for what it proves).
    constant DEPTH_D : integer := 16;
    variable beats_d : integer;
    variable lasts_d : integer;
    variable guard_d : integer;
    variable ok_d    : boolean;

  begin
    rstn_i <= '0';
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    rstn_i <= '1';
    wait until rising_edge(clk_i);

    -- Thresholds are now BELOW the CFD crossing value (40) for ascending ramps and above its
    -- negative for descending ones. They have to be: the threshold only ARMS the discriminator,
    -- and if the crossing arrives first the pulse is never armed and nothing fires. The old
    -- values (90..356) all sat above the crossing and would have produced a silent no-trigger --
    -- the same sensitivity trap cfd_trigger.vhd's header describes.
    run_test("negative-going small", -20, '0', 4, 16, false);
    run_test("positive-going small", 20, '1', 8, 32, true);
    run_test("positive-going with backpressure", 20, '1', 8, 32, true, true);
    -- delay=4, not 2: firmware now rejects anything below the CFD's pipeline latency, because
    -- the captured window would not contain the trigger point. 4 is the new minimum.
    run_test("boundary delay=4 (minimum)", 20, '1', 4, 16, true);
    run_test("boundary delay=256", 20, '1', 256, 300, true);
    run_test("boundary depth=4096", 20, '1', 4, 4096, true);
    -- Mirrors the real system: depth 1024 with stalls the length of an fci_core frame.
    run_test("long stall (fci_core-like), depth=1024", 20, '1', 100, 1024, true, false, true);

    -- Reconfiguration hazard: on real hardware, reprogramming threshold/polarity while
    -- adc_data_i doesn't move produced spurious "triggered" captures full of pure baseline noise
    -- -- trigger.vhd's `above` was being compared across a threshold/polarity change boundary,
    -- which can look exactly like a genuine crossing. Reproduces the same sequence here: hold
    -- adc_data_i fixed and drive threshold/polarity through transitions that would have crossed
    -- the old (leftover) comparator state, with monitor_no_spurious watching for any capture.
    ---------------------------------------------------------------------------
    -- The point of double-buffering: a second trigger arriving while the first trace is still
    -- draining must be ACCEPTED, not dropped. Single-buffered, armed_o was high only in IDLE, so
    -- every event during a stream was lost -- half the live time at full rate.
    --
    -- armed_o is internal to trigger_core_top, so this is checked behaviourally: hold tready low
    -- so nothing drains, fire two triggers, then release tready and require TWO complete traces
    -- (2 x depth beats, exactly 2 TLASTs). A single-buffered core yields one.
    report "=== Test: second trigger during stream is captured (double buffering) ===";
    test_count <= test_count + 1;
    beats_d := 0; lasts_d := 0; guard_d := 0; ok_d := true;

    m_axis_tready <= '0';
    axi_write(0, 20);
    axi_write(4, 1);
    axi_write(8, 4);
    axi_write(12, DEPTH_D);
    axi_write(16, CFD_FRAC_V);
    axi_write(20, CFD_DELAY_V);

    -- Baseline is ZERO between steps, not a DC offset. The CFD needs it: cfd = s[n-D] - f*s[n]
    -- rests at b*(1-f), so from a baseline of b=100 the bipolar signal never goes negative and
    -- there is no zero crossing to find -- an earlier version of this scenario stepped 100 -> 200
    -- and produced no triggers at all. blr_core restores the baseline to zero in the real chain,
    -- which is exactly what makes the CFD usable there; see cfd_trigger.vhd's header.
    --
    -- Each step must also be held longer than the CFD delay so the delayed copy catches up and
    -- the bipolar signal returns to rest before the next step.
    adc_data_i <= ob_to_2c(0);
    for i in 0 to CFD_DELAY_V + 12 loop
      wait until rising_edge(clk_i);
    end loop;
    adc_data_i <= ob_to_2c(200);            -- first crossing
    for i in 0 to DEPTH_D + 20 loop
      wait until rising_edge(clk_i);
    end loop;

    adc_data_i <= ob_to_2c(0);
    for i in 0 to CFD_DELAY_V + 12 loop
      wait until rising_edge(clk_i);
    end loop;
    adc_data_i <= ob_to_2c(200);            -- second crossing, first trace still undrained
    for i in 0 to DEPTH_D + 20 loop
      wait until rising_edge(clk_i);
    end loop;
    adc_data_i <= ob_to_2c(0);

    m_axis_tready <= '1';
    while lasts_d < 2 and guard_d < 20 * DEPTH_D loop
      wait until rising_edge(clk_i);
      guard_d := guard_d + 1;
      if m_axis_tvalid = '1' and m_axis_tready = '1' then
        beats_d := beats_d + 1;
        if m_axis_tlast = '1' then
          lasts_d := lasts_d + 1;
        end if;
      end if;
    end loop;

    if lasts_d /= 2 then
      ok_d := false;
      report "  FAIL: expected 2 traces, got " & integer'image(lasts_d)
             & " (a single-buffered core drops the second trigger)";
    end if;
    if beats_d /= 2 * DEPTH_D then
      ok_d := false;
      report "  FAIL: expected " & integer'image(2 * DEPTH_D) & " beats, got "
             & integer'image(beats_d);
    end if;
    if ok_d then
      report "  PASS (2 traces, " & integer'image(beats_d)
             & " beats -- second trigger accepted during stream)";
    else
      fail_count <= fail_count + 1;
      report "  Test 'double buffering' FAILED" severity error;
    end if;
    wait until rising_edge(clk_i);

    ---------------------------------------------------------------------------
    report "=== Test: reconfiguration hazard (no spurious capture) ===";
    test_count    <= test_count + 1;
    m_axis_tready <= '1';

    -- Small, known depth/delay first (unrelated to the threshold/polarity hazard itself) so
    -- that if the upcoming adc_data_i jump causes one real, legitimate trigger under whatever
    -- threshold/polarity the previous test left behind, its capture drains quickly instead of
    -- running for a leftover depth of up to 4096.
    axi_write(8, 4);
    axi_write(12, 16);

    -- Settle at a ZERO baseline, and hold it comfortably longer than the CFD delay before
    -- declaring that no capture may occur.
    --
    -- Both parts matter. Zero because the CFD only rests at zero (see the double-buffering
    -- scenario above). Settled because the STEP onto the baseline is itself a legitimate CFD
    -- stimulus: an earlier version stepped to 5000 and asserted expect_no_capture 40 cycles
    -- later, and the delayed copy was still catching up, so the resulting genuine trigger was
    -- reported as a spurious one. The hazard under test is reconfiguration with the input held
    -- STILL -- so the input has to actually be still, in the CFD's terms, before the test starts.
    adc_data_i <= ob_to_2c(0);
    for i in 0 to CFD_DELAY_V + 40 loop
      wait until rising_edge(clk_i);
    end loop;

    expect_no_capture <= true;

    axi_write(0, 9000); -- threshold -> 9000: baseline (0) < 9000, arming settles to '0'
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

    axi_write(0, 16383); -- threshold 0 -> 16383: FALLING hazard (arming would jump 1->0 with
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
