-- Self-checking testbench for blr_core_top. Same pass/fail reporting convention as
-- trigger_core_tb: each scenario reports PASS/FAIL, and a count is printed at the end.
--
-- The scenarios target the ways a gated baseline restorer actually fails in the field, not just
-- the happy path: a gate that locks out at cold start, a gate that locks out after a DC step,
-- pulses dragging the estimate upward, and an output that wraps instead of clamping.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity blr_core_tb is
end entity blr_core_tb;

architecture sim of blr_core_tb is

  constant ADC_WIDTH : integer := 14;
  -- Signed 14-bit range. The datapath is signed end to end now: the ADC's 2's-complement word is
  -- used as-is and the restored output is centred on zero.
  constant ADC_MAX : integer := 2 ** (ADC_WIDTH - 1) - 1;  --  8191
  constant ADC_MIN : integer := -(2 ** (ADC_WIDTH - 1));   -- -8192
  constant CLK_PERIOD : time := 20 ns; -- 50 MHz, matching clk_adc

  signal clk_i  : std_logic := '0';
  signal rstn_i : std_logic := '0';

  signal adc_data_i : std_logic_vector(ADC_WIDTH - 1 downto 0) := (others => '0');

  signal s_axi_awaddr  : std_logic_vector(4 downto 0) := (others => '0');
  signal s_axi_awvalid : std_logic := '0';
  signal s_axi_awready : std_logic;
  signal s_axi_wdata   : std_logic_vector(31 downto 0) := (others => '0');
  signal s_axi_wstrb   : std_logic_vector(3 downto 0) := "1111";
  signal s_axi_wvalid  : std_logic := '0';
  signal s_axi_wready  : std_logic;
  signal s_axi_bresp   : std_logic_vector(1 downto 0);
  signal s_axi_bvalid  : std_logic;
  signal s_axi_bready  : std_logic := '1';
  signal s_axi_araddr  : std_logic_vector(4 downto 0) := (others => '0');
  signal s_axi_arvalid : std_logic := '0';
  signal s_axi_arready : std_logic;
  signal s_axi_rdata   : std_logic_vector(31 downto 0);
  signal s_axi_rresp   : std_logic_vector(1 downto 0);
  signal s_axi_rvalid  : std_logic;
  signal s_axi_rready  : std_logic := '1';

  signal m_axis_tdata  : std_logic_vector(15 downto 0);
  signal m_axis_tvalid : std_logic;
  -- Held LOW for the whole run on purpose: this core must ignore backpressure entirely, because
  -- there is no buffer between it and the ADC pins. If any check below still passes with tready
  -- low, the core is genuinely free-running rather than accidentally working.
  signal m_axis_tready : std_logic := '0';
  signal baseline_o  : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal gate_open_o : std_logic;

  signal test_count : integer := 0;
  signal fail_count : integer := 0;

  -- The ADC's 2's-complement word for a given signed level. No conversion any more -- the pins
  -- carry signed data and the core uses it directly -- so this is just a signed cast, kept as a
  -- function so the scenarios below read as "drive this level".
  function adc_word(v : integer) return std_logic_vector is
  begin
    return std_logic_vector(to_signed(v, ADC_WIDTH));
  end function adc_word;

begin

  clk_i <= not clk_i after CLK_PERIOD / 2;

  uut : entity work.blr_core_top
    generic map (
      ADC_WIDTH => ADC_WIDTH,
      ADC_IS_2C => true,
      MAX_SHIFT => 15
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
      m_axis_tvalid => m_axis_tvalid,
      m_axis_tready => m_axis_tready,
      baseline_o    => baseline_o,
      gate_open_o   => gate_open_o
    );

  stim : process

    procedure axi_write(addr : integer; val : integer) is
    begin
      wait until rising_edge(clk_i);
      s_axi_awaddr  <= std_logic_vector(to_unsigned(addr, 5));
      s_axi_wdata   <= std_logic_vector(to_unsigned(val, 32));
      s_axi_awvalid <= '1';
      s_axi_wvalid  <= '1';
      wait until rising_edge(clk_i) and s_axi_awready = '1';
      s_axi_awvalid <= '0';
      s_axi_wvalid  <= '0';
      wait until rising_edge(clk_i);
    end procedure axi_write;

    procedure axi_read(addr : integer; result : out integer) is
    begin
      wait until rising_edge(clk_i);
      s_axi_araddr  <= std_logic_vector(to_unsigned(addr, 5));
      s_axi_arvalid <= '1';
      wait until rising_edge(clk_i) and s_axi_arready = '1';
      s_axi_arvalid <= '0';
      wait until rising_edge(clk_i) and s_axi_rvalid = '1';
      result := to_integer(unsigned(s_axi_rdata));
      wait until rising_edge(clk_i);
    end procedure axi_read;

    -- Hold a constant offset-binary level on the ADC pins for n cycles.
    procedure drive_dc(level : integer; n : integer) is
    begin
      for i in 1 to n loop
        adc_data_i <= adc_word(level);
        wait until rising_edge(clk_i);
      end loop;
    end procedure drive_dc;

    -- One pulse: rise to level+amp, exponential-ish decay back, then quiet.
    procedure drive_pulse(level : integer; amp : integer; quiet : integer) is
      variable v : integer;
    begin
      for i in 0 to 69 loop
        v := level + (amp * (70 - i)) / 70;
        adc_data_i <= adc_word(v);
        wait until rising_edge(clk_i);
      end loop;
      drive_dc(level, quiet);
    end procedure drive_pulse;

    -- Reset with the ADC pins already holding `level`. The estimator seeds itself from the first
    -- sample after reset, so releasing reset while the pins still carry the previous scenario's
    -- value would seed from that instead -- which is exactly what the first run of this testbench
    -- did, seeding 8192 (all-zero pins, MSB-inverted) and failing every downstream check.
    procedure do_reset(level : integer) is
    begin
      adc_data_i <= adc_word(level);
      rstn_i     <= '0';
      for i in 0 to 4 loop
        wait until rising_edge(clk_i);
      end loop;
      rstn_i <= '1';
      wait until rising_edge(clk_i);
    end procedure do_reset;

    procedure check(name : string; ok : boolean) is
    begin
      test_count <= test_count + 1;
      wait until rising_edge(clk_i);
      if ok then
        report "  PASS: " & name;
      else
        fail_count <= fail_count + 1;
        report "  Test '" & name & "' FAILED" severity error;
      end if;
    end procedure check;

    variable rd        : integer;
    variable bl        : integer;
    variable out_v     : integer;
    variable ok_v      : boolean;
    variable min_bl    : integer;
    variable max_bl    : integer;

  begin
    do_reset(4000);

    ---------------------------------------------------------------------------
    report "=== Test: AXI4-Lite register write/read ===";
    axi_write(0, 6);      -- shift k = 6
    axi_write(4, 150);    -- gate threshold
    axi_write(8, 2);      -- ctrl: hold = 1
    axi_write(16, 200);   -- holdoff
    axi_read(0, rd);  ok_v := (rd = 6);
    axi_read(4, rd);  ok_v := ok_v and (rd = 150);
    axi_read(8, rd);  ok_v := ok_v and (rd = 2);
    axi_read(16, rd); ok_v := ok_v and (rd = 200);
    check("register write/read", ok_v);
    axi_write(8, 0); -- release hold

    ---------------------------------------------------------------------------
    report "=== Test: cold start seeds the estimate (gate must not lock out) ===";
    -- The whole point: baseline starts at 0, a real DC level is thousands of counts away, and a
    -- naive gated EMA would shut the gate on sample one and never recover.
    do_reset(-3000);
    axi_write(0, 4);
    axi_write(4, 100);
    axi_write(16, 128); -- hold-off: 128 > the 70-sample synthetic pulse below, with margin
    drive_dc(-3000, 20);
    bl := to_integer(signed(baseline_o));
    check("cold start seeds baseline to the first sample (got "
          & integer'image(bl) & ", expected ~-3000)", abs(bl - (-3000)) <= 2);

    ---------------------------------------------------------------------------
    report "=== Test: restored output sits at ZERO ===";
    -- The defining property of the signed design: whatever DC the detector sits at, a quiet input
    -- comes out at zero, with no mid-scale constant for downstream blocks to subtract back off.
    drive_dc(-3000, 200);
    out_v := to_integer(signed(m_axis_tdata));
    check("quiet baseline restores to zero (got " & integer'image(out_v) & ")",
          abs(out_v) <= 2);

    ---------------------------------------------------------------------------
    report "=== Test: small DC step is tracked ===";
    -- Step smaller than the gate threshold: the gate stays open and the EMA should follow.
    drive_dc(-2950, 2000);
    bl := to_integer(signed(baseline_o));
    check("baseline tracks a within-gate DC step (got " & integer'image(bl)
          & ", expected ~-2950)", abs(bl - (-2950)) <= 3);

    ---------------------------------------------------------------------------
    report "=== Test: pulses do NOT drag the baseline ===";
    -- The core requirement. Pulses are far larger than the gate threshold, so the gate must shut
    -- for their duration and the estimate must stay put.
    drive_dc(-2950, 500);
    min_bl := to_integer(signed(baseline_o));
    max_bl := min_bl;
    for p in 1 to 6 loop
      drive_pulse(-2950, 3000, 400);
      bl := to_integer(signed(baseline_o));
      if bl < min_bl then min_bl := bl; end if;
      if bl > max_bl then max_bl := bl; end if;
    end loop;
    check("baseline holds across 6 pulses (drift " & integer'image(max_bl - min_bl)
          & " counts, range " & integer'image(min_bl) & ".." & integer'image(max_bl) & ")",
          (max_bl - min_bl) <= 4 and abs(max_bl - (-2950)) <= 6);

    ---------------------------------------------------------------------------
    report "=== Test: pulse amplitude survives restoration ===";
    -- A pulse of amplitude A on any baseline must read as exactly A, with no offset term.
    adc_data_i <= adc_word(-2950 + 2000);
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    out_v := to_integer(signed(m_axis_tdata));
    check("pulse of 2000 reads as 2000 (got " & integer'image(out_v) & ")",
          abs(out_v - 2000) <= 8);
    drive_dc(-2950, 200);

    ---------------------------------------------------------------------------
    report "=== Test: full-scale excursions cannot overflow the output ===";
    -- With a signed output there is nothing to clamp: sample and baseline are both ADC_WIDTH-bit
    -- signed, so their difference spans at most +/-16383 and always fits the 16-bit output. These
    -- two checks drive the extremes and require the EXACT difference -- a wrapped result would
    -- come back with the opposite sign, which is precisely the fold signature that dominated
    -- bring-up. Structurally impossible here rather than guarded by a comparator.
    adc_data_i <= adc_word(ADC_MAX);
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    out_v := to_integer(signed(m_axis_tdata));
    check("max positive excursion is exact (got " & integer'image(out_v) & ", expected "
          & integer'image(ADC_MAX - (-2950)) & ")", out_v = ADC_MAX - (-2950));

    adc_data_i <= adc_word(ADC_MIN);
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    out_v := to_integer(signed(m_axis_tdata));
    check("max negative excursion is exact and negative (got " & integer'image(out_v)
          & ", expected " & integer'image(ADC_MIN - (-2950)) & ")",
          out_v = ADC_MIN - (-2950));
    drive_dc(-2950, 200);

    ---------------------------------------------------------------------------
    report "=== Test: watchdog recovers from a gate lock-out ===";
    -- A DC step LARGER than the gate threshold shuts the gate around a stale estimate. Without the
    -- watchdog the core would never recover; with it, the estimate must bleed to the new level.
    do_reset(-3000);
    axi_write(0, 4);
    axi_write(4, 100);
    axi_write(16, 8);  -- short hold-off so the watchdog limit stays 2^(k+3) = 128 cycles
    drive_dc(-3000, 50);
    drive_dc(3000, 12000);
    bl := to_integer(signed(baseline_o));
    check("watchdog drags baseline across an out-of-gate step (got " & integer'image(bl)
          & ", expected ~3000)", abs(bl - 3000) <= 50);

    ---------------------------------------------------------------------------
    report "=== Test: changing k preserves the estimate ===";
    -- acc is scaled by 2^k, so a naive k change would halve or double the baseline.
    do_reset(4000);
    axi_write(0, 4);
    axi_write(4, 100);
    axi_write(16, 8);
    drive_dc(4000, 400);
    bl := to_integer(signed(baseline_o));
    axi_write(0, 8); -- k 4 -> 8
    drive_dc(4000, 20);
    rd := to_integer(signed(baseline_o));
    check("baseline survives a runtime k change (" & integer'image(bl) & " -> "
          & integer'image(rd) & ")", abs(rd - bl) <= 2);

    ---------------------------------------------------------------------------
    report "=== Test: bypass forwards the converted sample ===";
    axi_write(8, 1); -- bypass
    drive_dc(4321, 20);
    out_v := to_integer(signed(m_axis_tdata));
    check("bypass passes the sample through (got " & integer'image(out_v)
          & ", expected 4321)", out_v = 4321);
    axi_write(8, 0);

    ---------------------------------------------------------------------------
    report "=== Test: stream is free-running (tready ignored, tvalid always high) ===";
    -- tready has been '0' for this entire run. Every check above therefore already ran against a
    -- stalled consumer; this one states the contract explicitly.
    axi_write(8, 0);
    drive_dc(4000, 10);
    ok_v := (m_axis_tvalid = '1');
    drive_dc(4321, 10);
    ok_v := ok_v and (to_integer(signed(m_axis_tdata)) /= 0);
    m_axis_tready <= '1';
    drive_dc(4321, 10);
    ok_v := ok_v and (m_axis_tvalid = '1');
    check("tvalid stays high and data advances regardless of tready", ok_v);

    ---------------------------------------------------------------------------
    wait until rising_edge(clk_i);
    report "=== " & integer'image(test_count) & " tests run, " & integer'image(fail_count)
           & " failed ===";
    if fail_count = 0 then
      report "TEST PASSED";
    else
      report "TEST FAILED" severity error;
    end if;
    wait;
  end process stim;

end architecture sim;
