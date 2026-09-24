-- Self-checking testbench for pulse_shaper_core_top. Style mirrors psd_core/tb/psd_core_tb.vhd
-- (same axi_write/axi_read procedures, same register-write-then-read-back opening test, same
-- overflow/watermark/FIFO-ordering coverage) so the two testbenches read the same way.
--
-- The filter-specific tests feed a genuine sampled exponential pulse, x[n] = A0*exp(-n/tau) for
-- n>=trig (tau in samples), which is exactly the input shape the Jordanov-Knoll pole-zero
-- correction is designed for. For tau matched to the configured `decay`, the textbook result (the
-- reason this filter exists at all -- Jordanov & Knoll, NIM A345 (1994) 337) is that the shaped
-- output reaches an EXACTLY FLAT plateau of height A0*peaking, independent of tau. That gives a
-- known-correct expected value from arithmetic (A0*peaking) rather than a second implementation of
-- the filter that could share a bug with the one under test -- same testing philosophy
-- psd_core_tb.vhd's own header comment states. A mismatched decay is checked qualitatively: it must
-- move the plateau measurably away from the matched-decay case, which is the actual proof the
-- pole-zero term is doing something rather than merely compiling.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

entity pulse_shaper_core_tb is
end entity pulse_shaper_core_tb;

architecture sim of pulse_shaper_core_tb is

  constant DATA_WIDTH : integer := 16;
  constant ACC_WIDTH  : integer := 32;
  constant FIFO_DEPTH : integer := 32;
  constant K_MAX      : integer := 256; -- matches the core's own default; 5.12 us at 50 Msps
  constant M_MAX      : integer := 256;
  constant DECAY_BITS : integer := 9;
  constant CLK_PERIOD : time := 20 ns;

  -- trapezoidal_filter.vhd is now a 4-stage pipeline (Stage 1's pole-zero multiply across two
  -- registered stages, then Tr' itself, then the double-difference d[n], each its own registered
  -- stage -- needed to close timing at this design's actual 150 MHz/6.667 ns clock, see that
  -- file's own header) rather than one combinational expression, so result_valid_o for a given
  -- frame's last beat now lands 4 cycles later than the original, unpipelined design.
  -- send_flat_frame/send_exp_frame below carry this many extra idle cycles past the frame's own
  -- boundary so every caller -- not just the ones that happen to have enough incidental margin
  -- from a multi-cycle AXI read afterward -- sees the result already published before checking it.
  constant RESULT_LATENCY_CYCLES : integer := 4;

  -- Register offsets (see pulse_shaper_axi4lite_regs.vhd)
  constant R_PEAKING  : integer := 16#00#;
  constant R_FLATTOP  : integer := 16#04#;
  constant R_DECAY    : integer := 16#08#;
  constant R_ENABLE   : integer := 16#0C#;
  constant R_CTRL     : integer := 16#10#;
  constant R_STATUS   : integer := 16#14#;
  constant R_AMP      : integer := 16#18#;
  constant R_TS_LO    : integer := 16#1C#;
  constant R_TS_HI    : integer := 16#20#;
  constant R_COUNT    : integer := 16#24#;
  constant R_WATERMARK : integer := 16#28#;
  constant R_DECAY_RECIP : integer := 16#2C#;
  constant RECIP_FRAC_BITS : real := 65536.0; -- 2**16, matching PULSE_SHAPER_DECAY_RECIP_FRAC_BITS

  signal clk_i  : std_logic := '0';
  signal rstn_i : std_logic := '0';

  signal s_axis_tdata  : std_logic_vector(15 downto 0) := (others => '0');
  signal s_axis_tuser  : std_logic_vector(63 downto 0) := (others => '0');
  signal s_axis_tlast  : std_logic := '0';
  signal s_axis_tvalid : std_logic := '0';
  signal s_axis_tready : std_logic;

  signal s_axi_awaddr  : std_logic_vector(5 downto 0) := (others => '0');
  signal s_axi_awvalid : std_logic := '0';
  signal s_axi_awready : std_logic;
  signal s_axi_wdata   : std_logic_vector(31 downto 0) := (others => '0');
  signal s_axi_wstrb   : std_logic_vector(3 downto 0) := "1111";
  signal s_axi_wvalid  : std_logic := '0';
  signal s_axi_wready  : std_logic;
  signal s_axi_bresp   : std_logic_vector(1 downto 0);
  signal s_axi_bvalid  : std_logic;
  signal s_axi_bready  : std_logic := '1';
  signal s_axi_araddr  : std_logic_vector(5 downto 0) := (others => '0');
  signal s_axi_arvalid : std_logic := '0';
  signal s_axi_arready : std_logic;
  signal s_axi_rdata   : std_logic_vector(31 downto 0);
  signal s_axi_rresp   : std_logic_vector(1 downto 0);
  signal s_axi_rvalid  : std_logic;
  signal s_axi_rready  : std_logic := '1';

  signal irq_o : std_logic;

  -- Non-power-of-2 delay-line regression, driven separately from the DUT above.
  --
  -- The DUT is instantiated at K_MAX=M_MAX=256, so nothing in the tests above exercises a
  -- MAX_DELAY that is not already a power of 2. 250 is kept here because it is what
  -- pulse_shaper_core_0 was actually built with in the block design when this broke -- the config
  -- has since moved to 256, but the component's contract is "any MAX_DELAY", and this is the case
  -- that proves it. Under the original variable_delay.vhd that combination wrote
  -- through an 8-bit pointer wrapping at 256 into an array declared 0 to 249, so six addresses per
  -- lap fell outside it: a bounds error in simulation, and in hardware a silent dependency on what
  -- the inferred BRAM did with the overhang. Vivado synthesis does not evaluate the assert that
  -- was supposed to catch it, so it reached the board.
  --
  -- Checked with a free-running ramp rather than a single pulse: the defect only appears once
  -- write_ptr has lapped, so the sample that comes back has to be right across many laps, not just
  -- on the first pass through the array.
  constant VD_MAX   : integer := 250;
  constant VD_MOD   : integer := 4096; -- ramp wrap; > 2*VD_MAX so the difference below is unambiguous
  signal vd_en      : std_logic := '0';
  signal vd_delay   : std_logic_vector(7 downto 0) := (others => '0'); -- clog2(250) = 8 bits
  signal vd_din     : std_logic_vector(15 downto 0) := (others => '0');
  signal vd_dout    : std_logic_vector(15 downto 0);

  signal test_count : integer := 0;
  signal fail_count : integer := 0;

  -- Watches tready for the whole run: this core must never stall the lockstep broadcaster.
  signal tready_ever_low : boolean := false;

begin

  clk_i <= not clk_i after CLK_PERIOD / 2;

  monitor_tready : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '1' and s_axis_tready /= '1' then
        tready_ever_low <= true;
      end if;
    end if;
  end process monitor_tready;

  -- Free-running ramp feeding the standalone delay line: data_i is simply "the cycle number",
  -- so the sample coming back out identifies exactly how many cycles ago it went in.
  vd_ramp : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        vd_din <= (others => '0');
      elsif vd_en = '1' then
        vd_din <= std_logic_vector(to_unsigned((to_integer(unsigned(vd_din)) + 1) mod VD_MOD, 16));
      end if;
    end if;
  end process vd_ramp;

  vd_uut : entity work.variable_delay
    generic map (
      DATA_WIDTH => 16,
      MAX_DELAY  => VD_MAX
    )
    port map (
      clk_i       => clk_i,
      rstn_i      => rstn_i,
      en_i        => vd_en,
      delay_sel_i => vd_delay,
      data_i      => vd_din,
      data_o      => vd_dout
    );

  uut : entity work.pulse_shaper_core_top
    generic map (
      DATA_WIDTH => DATA_WIDTH,
      ACC_WIDTH  => ACC_WIDTH,
      FIFO_DEPTH => FIFO_DEPTH,
      K_MAX      => K_MAX,
      M_MAX      => M_MAX,
      DECAY_BITS => DECAY_BITS
    )
    port map (
      clk_i         => clk_i,
      rstn_i        => rstn_i,
      s_axis_tdata  => s_axis_tdata,
      s_axis_tuser  => s_axis_tuser,
      s_axis_tlast  => s_axis_tlast,
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
      irq_o         => irq_o
    );

  stim : process

    -- to_signed, not to_unsigned: decay_recip is a negative fixed-point value, and to_signed
    -- produces the identical bit pattern to_unsigned would for every other (non-negative) use in
    -- this testbench, so this is a strict generalization, not a behavior change for existing calls.
    procedure axi_write(addr : integer; val : integer) is
    begin
      wait until rising_edge(clk_i);
      s_axi_awaddr  <= std_logic_vector(to_unsigned(addr, 6));
      s_axi_wdata   <= std_logic_vector(to_signed(val, 32));
      s_axi_awvalid <= '1';
      s_axi_wvalid  <= '1';
      wait until rising_edge(clk_i) and s_axi_awready = '1';
      s_axi_awvalid <= '0';
      s_axi_wvalid  <= '0';
      wait until rising_edge(clk_i);
    end procedure axi_write;

    -- Writes BOTH decay registers, mirroring what PulseShaper_Configure()/shaper_set() do in
    -- firmware: R_DECAY (samples, CLI-visible bookkeeping) and R_DECAY_RECIP (+1/tau in Q2.16,
    -- what the filter's pole-zero correction actually uses). Direct AXI-lite writes from this
    -- testbench bypass firmware entirely, so without this the filter would silently keep whatever
    -- decay_recip the previous call (or the power-on default) left behind.
    procedure configure_decay(tau_samples : real) is
    begin
      axi_write(R_DECAY, integer(round(tau_samples)));
      axi_write(R_DECAY_RECIP, integer(round(RECIP_FRAC_BITS / tau_samples)));
    end procedure configure_decay;

    procedure axi_read(addr : integer; result : out integer) is
    begin
      wait until rising_edge(clk_i);
      s_axi_araddr  <= std_logic_vector(to_unsigned(addr, 6));
      s_axi_arvalid <= '1';
      wait until rising_edge(clk_i) and s_axi_arready = '1';
      s_axi_arvalid <= '0';
      wait until rising_edge(clk_i) and s_axi_rvalid = '1';
      result := to_integer(signed(s_axi_rdata));
      wait until rising_edge(clk_i);
    end procedure axi_read;

    -- A frame whose every sample sits `dev` counts above baseline -- same helper shape as
    -- psd_core_tb.vhd's send_flat_frame, used here just to exercise ctrl/status/FIFO/watermark.
    procedure send_flat_frame(nbeats : integer; dev : integer; ts : integer) is
    begin
      for i in 0 to nbeats - 1 loop
        s_axis_tdata  <= std_logic_vector(to_signed(dev, 16));
        s_axis_tuser  <= std_logic_vector(to_unsigned(ts, 64));
        s_axis_tvalid <= '1';
        if i = nbeats - 1 then
          s_axis_tlast <= '1';
        else
          s_axis_tlast <= '0';
        end if;
        wait until rising_edge(clk_i);
      end loop;
      s_axis_tvalid <= '0';
      s_axis_tlast  <= '0';
      for i in 0 to RESULT_LATENCY_CYCLES loop
        wait until rising_edge(clk_i);
      end loop;
    end procedure send_flat_frame;

    -- Sampled exponential pulse: 0 before `trig`, A0*exp(-(n-trig)/tau) from `trig` onward. This is
    -- exactly the shape a charge-sensitive preamp's exponential discharge produces, and exactly the
    -- shape the pole-zero correction (the `decay` parameter) is derived to compensate.
    procedure send_exp_frame(nbeats : integer; trig : integer; a0 : real; tau : real;
                             ts : integer) is
      variable v : integer;
      variable sample_real : real;
    begin
      for i in 0 to nbeats - 1 loop
        if i < trig then
          v := 0;
        else
          sample_real := a0 * exp(-1.0 * real(i - trig) / tau);
          v := integer(round(sample_real));
        end if;
        s_axis_tdata  <= std_logic_vector(to_signed(v, 16));
        s_axis_tuser  <= std_logic_vector(to_unsigned(ts, 64));
        s_axis_tvalid <= '1';
        if i = nbeats - 1 then
          s_axis_tlast <= '1';
        else
          s_axis_tlast <= '0';
        end if;
        wait until rising_edge(clk_i);
      end loop;
      s_axis_tvalid <= '0';
      s_axis_tlast  <= '0';
      for i in 0 to RESULT_LATENCY_CYCLES loop
        wait until rising_edge(clk_i);
      end loop;
    end procedure send_exp_frame;

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

    variable rd, amp, tlo, thi, lvl : integer;
    variable lag_v : integer; -- measured delay-line lag, in cycles (non-power-of-2 test below)
    variable ok_v : boolean;
    variable amp_matched, amp_mismatched : integer;

  begin
    rstn_i <= '0';
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    rstn_i <= '1';
    wait until rising_edge(clk_i);

    ---------------------------------------------------------------------------
    report "=== Test: power-on reset defaults (measured-pulse-shape derived) ===";
    axi_read(R_PEAKING, rd);  ok_v := (rd = 50);
    axi_read(R_FLATTOP, rd);  ok_v := ok_v and (rd = 20);
    axi_read(R_DECAY, rd);    ok_v := ok_v and (rd = 245);
    axi_read(R_ENABLE, rd);   ok_v := ok_v and (rd = 1);
    check("reset defaults are peaking=50 flat_top=20 decay=245 enable=1", ok_v);

    ---------------------------------------------------------------------------
    report "=== Test: AXI4-Lite register write/read, including out-of-range saturation ===";
    axi_write(R_PEAKING, 40);
    axi_write(R_FLATTOP, 10);
    axi_write(R_DECAY, 100);
    axi_write(R_WATERMARK, 4);
    axi_read(R_PEAKING, rd); ok_v := (rd = 40);
    axi_read(R_FLATTOP, rd); ok_v := ok_v and (rd = 10);
    axi_read(R_DECAY, rd);   ok_v := ok_v and (rd = 100);
    axi_read(R_WATERMARK, rd); ok_v := ok_v and (rd = 4);
    check("register write/read", ok_v);

    -- Readback shows the RAW value as written, not the clamped one -- matching psd_axi4lite_regs
    -- and trigger_core's axi4lite_regs, which both read back depth_reg/decay_reg raw and clamp
    -- only on the *_o port the datapath actually consumes. This is deliberately NOT the same
    -- thing as "the write was silently ignored": an out-of-range write is still accepted and
    -- still visibly out-of-range on readback (9999 is obviously not a valid peaking value), it
    -- just isn't rewritten to the pinned value in the register itself.
    axi_write(R_PEAKING, 9999); -- above the 250 spec max
    axi_read(R_PEAKING, rd);
    check("peaking write is accepted and reads back as written (got " & integer'image(rd) & ")",
          rd = 9999);
    axi_write(R_PEAKING, 40); -- restore, for the tests below

    ---------------------------------------------------------------------------
    report "=== Test: matched decay yields a flat-top plateau of A0*peaking ===";
    -- peaking=40, flat_top=10, decay=200 (all comfortably inside a 400-beat frame). A0=1000,
    -- tau=200 samples matches `decay` exactly -- the textbook case where the pole-zero correction
    -- is exact and the plateau should land at A0*peaking = 40000, not just "somewhere positive".
    axi_write(R_PEAKING, 40);
    axi_write(R_FLATTOP, 10);
    configure_decay(200.0);
    axi_write(R_ENABLE, 1);
    axi_write(R_CTRL, 2); -- clear
    send_exp_frame(400, 50, 1000.0, 200.0, 16#1111#);
    axi_read(R_AMP, amp);
    amp_matched := amp;
    check("matched-decay plateau within 2% of A0*peaking=40000 (got " & integer'image(amp) & ")",
          amp > 39200 and amp < 40800);

    axi_read(R_TS_LO, tlo);
    axi_read(R_TS_HI, thi);
    check("timestamp travels with the result (lo=" & integer'image(tlo) & " hi="
          & integer'image(thi) & ")", tlo = 16#1111# and thi = 0);

    ---------------------------------------------------------------------------
    report "=== Test: mismatched decay measurably moves the plateau ===";
    -- Same pulse (A0=1000, tau=200), but `decay` set to half the true tau. If the pole-zero term
    -- were doing nothing, this would read the same as the matched case; the whole point of the
    -- correction is that it doesn't.
    configure_decay(100.0);
    axi_write(R_CTRL, 2);
    send_exp_frame(400, 50, 1000.0, 200.0, 16#2222#);
    axi_read(R_AMP, amp);
    amp_mismatched := amp;
    check("mismatched decay (100 vs true tau 200) moves the plateau away from the matched case "
          & "(matched=" & integer'image(amp_matched) & ", mismatched=" & integer'image(amp_mismatched)
          & ")", abs(amp_mismatched - amp_matched) > (amp_matched / 20)); -- >5% apart
    configure_decay(200.0);

    ---------------------------------------------------------------------------
    report "=== Test: enable=0 bypasses the shaper, tracks raw sample peak ===";
    axi_write(R_ENABLE, 0);
    axi_write(R_CTRL, 2);
    send_exp_frame(400, 50, 1000.0, 200.0, 16#3333#);
    axi_read(R_AMP, amp);
    check("disabled shaper reports the raw peak sample, not a shaped plateau (got "
          & integer'image(amp) & ", expected 1000)", amp = 1000);
    axi_write(R_ENABLE, 1);

    ---------------------------------------------------------------------------
    report "=== Test: flat_top=0 (triangular, no plateau) still produces a positive result ===";
    axi_write(R_FLATTOP, 0);
    axi_write(R_CTRL, 2);
    send_exp_frame(400, 50, 1000.0, 200.0, 16#4444#);
    axi_read(R_AMP, amp);
    check("flat_top=0 is accepted and produces a positive amplitude (got "
          & integer'image(amp) & ")", amp > 0);
    axi_write(R_FLATTOP, 10);

    -- No "back-to-back frames don't leak" test here: variable_delay.vhd's array is genuinely
    -- free-running (see its own header for why -- clearing it made no measurable difference to
    -- LUT cost either way), so a quiet frame immediately after a large pulse CAN see a transient
    -- from the previous frame's tail still in the pipeline for the first ~(peaking+flat_top)
    -- samples. That is accepted, not a bug this testbench should assert against.

    ---------------------------------------------------------------------------
    report "=== Test: FIFO buffers several events in order ===";
    axi_write(R_CTRL, 2);
    send_flat_frame(200, 1, 16#A1#);
    send_flat_frame(200, 2, 16#A2#);
    send_flat_frame(200, 3, 16#A3#);
    axi_read(R_STATUS, rd);
    lvl := (rd / 256) mod 2048;
    ok_v := (lvl = 3);
    axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (tlo = 16#A1#);
    axi_write(R_CTRL, 1); -- pop
    axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (tlo = 16#A2#);
    axi_write(R_CTRL, 1);
    axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (tlo = 16#A3#);
    axi_write(R_CTRL, 1);
    axi_read(R_STATUS, rd);
    ok_v := ok_v and ((rd mod 2) = 1); -- empty
    check("FIFO returns 3 events in order and empties", ok_v);

    ---------------------------------------------------------------------------
    report "=== Test: event counter ===";
    axi_read(R_COUNT, rd);
    check("event_count counts every frame (got " & integer'image(rd) & ", expected 3)", rd = 3);

    ---------------------------------------------------------------------------
    report "=== Test: overflow is flagged, never backpressured ===";
    axi_write(R_CTRL, 2);
    for i in 1 to FIFO_DEPTH + 4 loop
      send_flat_frame(150, 5, i);
    end loop;
    axi_read(R_STATUS, rd);
    ok_v := ((rd / 4) mod 2) = 1;         -- overflow sticky bit
    ok_v := ok_v and (((rd / 2) mod 2) = 1); -- full
    check("overflow flagged after " & integer'image(FIFO_DEPTH + 4) & " undrained events", ok_v);
    check("tready never deasserted (lockstep broadcaster must not stall)", not tready_ever_low);

    ---------------------------------------------------------------------------
    report "=== Test: clear resets FIFO, overflow and counter ===";
    axi_write(R_CTRL, 2);
    axi_read(R_STATUS, rd);
    ok_v := ((rd mod 2) = 1) and (((rd / 4) mod 2) = 0);
    axi_read(R_COUNT, rd);
    ok_v := ok_v and (rd = 0);
    check("clear empties the FIFO and clears overflow and count", ok_v);

    ---------------------------------------------------------------------------
    report "=== Test: watermark interrupt ===";
    axi_write(R_CTRL, 2);
    axi_write(R_WATERMARK, 3);
    send_flat_frame(150, 1, 1);
    send_flat_frame(150, 1, 2);
    ok_v := (irq_o = '0');
    send_flat_frame(150, 1, 3);
    wait until rising_edge(clk_i);
    wait until rising_edge(clk_i);
    ok_v := ok_v and (irq_o = '1');
    check("irq asserts at the watermark, not before", ok_v);

    ---------------------------------------------------------------------------
    report "=== Test: variable_delay with a non-power-of-2 MAX_DELAY (" & integer'image(VD_MAX)
           & ") ===";
    -- Run the ramp well past several laps of the 256-entry array before looking at anything, so a
    -- wraparound that addressed outside the old 250-entry one has had many chances to corrupt a
    -- tap. Each measured lag is (ramp value in) - (ramp value out), mod the ramp's own wrap.
    vd_en <= '1';
    for i in 0 to 1200 loop
      wait until rising_edge(clk_i);
    end loop;

    -- A mid-range tap first: this one addressed in bounds even under the old code, so it pins down
    -- the component's latency convention. If THIS fails, the tap contract moved, not the wrap.
    vd_delay <= std_logic_vector(to_unsigned(10, 8));
    for i in 0 to 300 loop
      wait until rising_edge(clk_i);
    end loop;
    lag_v := (to_integer(unsigned(vd_din)) - to_integer(unsigned(vd_dout)) + VD_MOD) mod VD_MOD;
    check("mid-range tap (10) returns the sample from 10 cycles ago (got " & integer'image(lag_v)
          & ")", lag_v = 10);

    -- 249 and 250 are the taps that reach furthest back, so they are the ones whose read_addr
    -- subtraction wraps past the array's end. 250 is also MAX_DELAY itself, the clamp's ceiling.
    for d in VD_MAX - 1 to VD_MAX loop
      vd_delay <= std_logic_vector(to_unsigned(d, 8));
      for i in 0 to 300 loop
        wait until rising_edge(clk_i);
      end loop;
      lag_v := (to_integer(unsigned(vd_din)) - to_integer(unsigned(vd_dout)) + VD_MOD) mod VD_MOD;
      check("deepest tap (" & integer'image(d) & ") returns the sample from " & integer'image(d)
            & " cycles ago (got " & integer'image(lag_v) & ")", lag_v = d);
    end loop;
    vd_en <= '0';

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
