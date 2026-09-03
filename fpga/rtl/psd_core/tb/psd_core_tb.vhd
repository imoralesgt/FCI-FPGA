-- Self-checking testbench for psd_core_top.
--
-- Frames here are short (256 beats) rather than the real 1024: the core's frame length is defined
-- entirely by tlast, so nothing under test depends on the real depth, and short frames keep the
-- run fast enough to iterate on.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity psd_core_tb is
end entity psd_core_tb;

architecture sim of psd_core_tb is

  constant DATA_WIDTH : integer := 16;
  constant ACC_WIDTH  : integer := 32;
  constant FIFO_DEPTH : integer := 32;
  -- Samples are signed and already restored to zero by blr_core, so a "deviation" is just the
  -- sample value and the reference is 0.
  constant BASELINE_REF : integer := 0;
  constant CLK_PERIOD : time := 20 ns;

  -- Register offsets
  constant R_PRE_TRIGGER : integer := 16#00#;
  constant R_PRE_GATE    : integer := 16#04#;
  constant R_SHORT_GATE  : integer := 16#08#;
  constant R_LONG_GATE   : integer := 16#0C#;
  constant R_BASELINE    : integer := 16#10#;
  constant R_CTRL        : integer := 16#14#;
  constant R_STATUS      : integer := 16#18#;
  constant R_ESHORT      : integer := 16#1C#;
  constant R_ELONG       : integer := 16#20#;
  constant R_TS_LO       : integer := 16#24#;
  constant R_TS_HI       : integer := 16#28#;
  constant R_COUNT       : integer := 16#2C#;
  constant R_WATERMARK   : integer := 16#30#;
  constant R_PEAK        : integer := 16#34#;

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

  signal test_count : integer := 0;
  signal fail_count : integer := 0;

  -- Watches tready for the whole run: a psd_core that ever deasserts it would stall the lockstep
  -- broadcaster and take fci_core and the raw-trace DMA down with it.
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

  uut : entity work.psd_core_top
    generic map (
      DATA_WIDTH => DATA_WIDTH,
      MAX_DEPTH  => 4096,
      ACC_WIDTH  => ACC_WIDTH,
      FIFO_DEPTH => FIFO_DEPTH
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

    procedure axi_write(addr : integer; val : integer) is
    begin
      wait until rising_edge(clk_i);
      s_axi_awaddr  <= std_logic_vector(to_unsigned(addr, 6));
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
      s_axi_araddr  <= std_logic_vector(to_unsigned(addr, 6));
      s_axi_arvalid <= '1';
      wait until rising_edge(clk_i) and s_axi_arready = '1';
      s_axi_arvalid <= '0';
      wait until rising_edge(clk_i) and s_axi_rvalid = '1';
      result := to_integer(signed(s_axi_rdata));
      wait until rising_edge(clk_i);
    end procedure axi_read;

    -- A frame whose every sample sits `dev` counts above mid-scale. Makes each integral exactly
    -- dev * (number of in-gate samples), so the expected value is arithmetic rather than a
    -- reference model that could share a bug with the design.
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
      wait until rising_edge(clk_i);
    end procedure send_flat_frame;

    -- Prompt spike at the trigger plus a long flat tail, so the short gate and the long gate see
    -- genuinely different charge -- which is the whole point of the two windows.
    procedure send_pulse_frame(nbeats : integer; trig : integer; prompt : integer;
                               tail : integer; ts : integer) is
      variable v : integer;
    begin
      for i in 0 to nbeats - 1 loop
        if i < trig then
          v := 0;
        elsif i < trig + 20 then
          v := prompt;
        else
          v := tail;
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
      wait until rising_edge(clk_i);
    end procedure send_pulse_frame;

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

    variable rd, es, el, tlo, thi, lvl, pk : integer;
    variable ok_v : boolean;

  begin
    rstn_i <= '0';
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    rstn_i <= '1';
    wait until rising_edge(clk_i);

    ---------------------------------------------------------------------------
    report "=== Test: AXI4-Lite register write/read ===";
    axi_write(R_PRE_TRIGGER, 100);
    axi_write(R_PRE_GATE,    32);
    axi_write(R_SHORT_GATE,  80);
    axi_write(R_LONG_GATE,   100);
    axi_write(R_BASELINE,    BASELINE_REF);
    axi_write(R_WATERMARK,   4);
    axi_read(R_PRE_TRIGGER, rd); ok_v := (rd = 100);
    axi_read(R_PRE_GATE, rd);    ok_v := ok_v and (rd = 32);
    axi_read(R_SHORT_GATE, rd);  ok_v := ok_v and (rd = 80);
    axi_read(R_LONG_GATE, rd);   ok_v := ok_v and (rd = 100);
    axi_read(R_BASELINE, rd);    ok_v := ok_v and (rd = BASELINE_REF);
    axi_read(R_WATERMARK, rd);   ok_v := ok_v and (rd = 4);
    check("register write/read", ok_v);

    ---------------------------------------------------------------------------
    report "=== Test: gate arithmetic on a flat frame ===";
    -- gate_start = 100 - 32 = 68. short = [68,148) = 80 samples, long = [68,168) = 100 samples.
    -- With every sample 10 counts above baseline: 800 and 1000 exactly.
    axi_write(R_CTRL, 2); -- clear
    send_flat_frame(256, 10, 16#1111#);
    axi_read(R_ESHORT, es);
    axi_read(R_ELONG, el);
    check("short gate integral = 80*10 (got " & integer'image(es) & ")", es = 800);
    check("long gate integral = 100*10 (got " & integer'image(el) & ")", el = 1000);
    axi_read(R_PEAK, pk);
    check("peak of a flat frame equals its constant deviation (got "
          & integer'image(pk) & ", expected 10)", pk = 10);

    ---------------------------------------------------------------------------
    report "=== Test: timestamp travels with the result ===";
    axi_read(R_TS_LO, tlo);
    axi_read(R_TS_HI, thi);
    check("timestamp preserved (lo=" & integer'image(tlo) & " hi=" & integer'image(thi) & ")",
          tlo = 16#1111# and thi = 0);

    ---------------------------------------------------------------------------
    report "=== Test: negative deviation subtracts ===";
    -- Undershoot below baseline must reduce the integral, not wrap it to a huge positive number.
    axi_write(R_CTRL, 2);
    send_flat_frame(256, -10, 16#2222#);
    axi_read(R_ESHORT, es);
    check("undershoot integrates negative (got " & integer'image(es) & ", expected -800)",
          es = -800);
    axi_read(R_PEAK, pk);
    check("peak of an all-negative frame is its true max, not clamped to 0 (got "
          & integer'image(pk) & ", expected -10)", pk = -10);

    ---------------------------------------------------------------------------
    report "=== Test: short and long gates differ on a real pulse shape ===";
    axi_write(R_CTRL, 2);
    -- Prompt 1000 for 20 samples from the trigger, tail 50 after. Short gate [68,148):
    -- 32 pre-trigger samples at 0, 20 at 1000, 28 at 50 = 21400. Long gate [68,168): adds 20 more
    -- tail samples = 22400.
    send_pulse_frame(256, 100, 1000, 50, 16#3333#);
    axi_read(R_ESHORT, es);
    axi_read(R_ELONG, el);
    check("short gate = 20*1000 + 28*50 (got " & integer'image(es) & ", expected 21400)",
          es = 21400);
    check("long gate = short + 20*50 (got " & integer'image(el) & ", expected 22400)",
          el = 22400);
    check("long gate exceeds short gate", el > es);
    axi_read(R_PEAK, pk);
    check("peak is the frame's max sample, independent of gate placement (got "
          & integer'image(pk) & ", expected 1000)", pk = 1000);

    ---------------------------------------------------------------------------
    report "=== Test: pre_gate = 0 starts integration at the trigger ===";
    axi_write(R_CTRL, 2);
    axi_write(R_PRE_GATE, 0);
    -- gate_start = 100. short = [100,180): 20 at 1000 + 60 at 50 = 23000.
    send_pulse_frame(256, 100, 1000, 50, 16#4444#);
    axi_read(R_ESHORT, es);
    check("pre_gate=0 integrates from the trigger (got " & integer'image(es)
          & ", expected 23000)", es = 23000);
    axi_write(R_PRE_GATE, 32);

    ---------------------------------------------------------------------------
    report "=== Test: FIFO buffers several events in order ===";
    axi_write(R_CTRL, 2);
    send_flat_frame(256, 1, 16#A1#);
    send_flat_frame(256, 2, 16#A2#);
    send_flat_frame(256, 3, 16#A3#);
    axi_read(R_STATUS, rd);
    lvl := (rd / 256) mod 64;
    ok_v := (lvl = 3);
    axi_read(R_ESHORT, es); axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (es = 80) and (tlo = 16#A1#);
    axi_write(R_CTRL, 1); -- pop
    axi_read(R_ESHORT, es); axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (es = 160) and (tlo = 16#A2#);
    axi_write(R_CTRL, 1);
    axi_read(R_ESHORT, es); axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (es = 240) and (tlo = 16#A3#);
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
      send_flat_frame(200, 5, i);
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
    report "=== Test: baseline_ref shifts the integral ===";
    -- Raising the reference by 1 count removes exactly one count per in-gate sample.
    axi_write(R_BASELINE, 1);
    send_flat_frame(256, 10, 16#5555#);
    axi_read(R_ESHORT, es);
    check("baseline_ref=1 removes 80 counts (got " & integer'image(es) & ", expected 720)",
          es = 720);
    axi_write(R_BASELINE, BASELINE_REF);

    ---------------------------------------------------------------------------
    report "=== Test: watermark interrupt ===";
    axi_write(R_CTRL, 2);
    axi_write(R_WATERMARK, 3);
    send_flat_frame(200, 1, 1);
    send_flat_frame(200, 1, 2);
    ok_v := (irq_o = '0');
    send_flat_frame(200, 1, 3);
    wait until rising_edge(clk_i);
    wait until rising_edge(clk_i);
    ok_v := ok_v and (irq_o = '1');
    check("irq asserts at the watermark, not before", ok_v);

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
