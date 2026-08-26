-- Self-checking testbench for fci_sink_top.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity fci_sink_tb is
end entity fci_sink_tb;

architecture sim of fci_sink_tb is

  constant ACC_WIDTH  : integer := 32;
  constant FIFO_DEPTH : integer := 32;
  constant CLK_PERIOD : time := 20 ns;

  constant R_CTRL   : integer := 16#00#;
  constant R_STATUS : integer := 16#04#;
  constant R_PSA_L  : integer := 16#08#;
  constant R_PSA_W  : integer := 16#0C#;
  constant R_TS_LO  : integer := 16#10#;
  constant R_TS_HI  : integer := 16#14#;
  constant R_COUNT  : integer := 16#18#;
  constant R_WMARK  : integer := 16#1C#;

  signal clk_i  : std_logic := '0';
  signal rstn_i : std_logic := '0';

  signal s_axis_tdata  : std_logic_vector(ACC_WIDTH - 1 downto 0) := (others => '0');
  signal s_axis_tuser  : std_logic_vector(63 downto 0) := (others => '0');
  signal s_axis_tlast  : std_logic := '0';
  signal s_axis_tvalid : std_logic := '0';
  signal s_axis_tready : std_logic;

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

  signal irq_o : std_logic;

  signal test_count : integer := 0;
  signal fail_count : integer := 0;
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

  uut : entity work.fci_sink_top
    generic map (ACC_WIDTH => ACC_WIDTH, FIFO_DEPTH => FIFO_DEPTH)
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

    procedure send_beat(val : integer; ts : integer; last : std_logic) is
    begin
      s_axis_tdata  <= std_logic_vector(to_unsigned(val, ACC_WIDTH));
      s_axis_tuser  <= std_logic_vector(to_unsigned(ts, 64));
      s_axis_tlast  <= last;
      s_axis_tvalid <= '1';
      wait until rising_edge(clk_i);
      s_axis_tvalid <= '0';
      s_axis_tlast  <= '0';
      wait until rising_edge(clk_i);
    end procedure send_beat;

    -- One complete fci_core event: PSA_l (tlast low) then PSA_w (tlast high).
    procedure send_event(l : integer; w : integer; ts : integer) is
    begin
      send_beat(l, ts, '0');
      send_beat(w, ts, '1');
    end procedure send_event;

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

    variable rd, l, w, tlo, thi, lvl : integer;
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
    axi_write(R_WMARK, 5);
    axi_read(R_WMARK, rd);
    check("watermark write/read", rd = 5);

    ---------------------------------------------------------------------------
    report "=== Test: two beats pair into one result ===";
    axi_write(R_CTRL, 2); -- clear
    send_event(111, 222, 16#ABC#);
    axi_read(R_PSA_L, l);
    axi_read(R_PSA_W, w);
    axi_read(R_TS_LO, tlo);
    axi_read(R_TS_HI, thi);
    check("psa_l/psa_w paired in order (l=" & integer'image(l) & " w=" & integer'image(w) & ")",
          l = 111 and w = 222);
    check("timestamp carried (lo=" & integer'image(tlo) & ")", tlo = 16#ABC# and thi = 0);

    ---------------------------------------------------------------------------
    report "=== Test: events drain in order ===";
    axi_write(R_CTRL, 2);
    send_event(1, 10, 16#A1#);
    send_event(2, 20, 16#A2#);
    send_event(3, 30, 16#A3#);
    axi_read(R_STATUS, rd);
    lvl  := (rd / 256) mod 64;
    ok_v := (lvl = 3);
    axi_read(R_PSA_L, l); axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (l = 1) and (tlo = 16#A1#);
    axi_write(R_CTRL, 1);
    axi_read(R_PSA_L, l); axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (l = 2) and (tlo = 16#A2#);
    axi_write(R_CTRL, 1);
    axi_read(R_PSA_W, w); axi_read(R_TS_LO, tlo);
    ok_v := ok_v and (w = 30) and (tlo = 16#A3#);
    axi_write(R_CTRL, 1);
    axi_read(R_STATUS, rd);
    ok_v := ok_v and ((rd mod 2) = 1);
    check("three events drain in order and empty", ok_v);

    ---------------------------------------------------------------------------
    report "=== Test: event counter ===";
    axi_read(R_COUNT, rd);
    check("event_count = 3 (got " & integer'image(rd) & ")", rd = 3);

    ---------------------------------------------------------------------------
    report "=== Test: single-beat event flags a framing error ===";
    -- If fci_core ever emitted one beat instead of two, silently pairing across event boundaries
    -- would invert the FCI ratio for every event afterwards. This must be visible instead.
    axi_write(R_CTRL, 2);
    send_beat(99, 16#BB#, '1');   -- lone beat with tlast
    axi_read(R_STATUS, rd);
    check("framing error flagged on a one-beat event", ((rd / 8) mod 2) = 1);

    ---------------------------------------------------------------------------
    report "=== Test: pairing re-synchronizes after a framing error ===";
    axi_write(R_CTRL, 2);
    send_event(7, 8, 16#CC#);
    axi_read(R_PSA_L, l);
    axi_read(R_PSA_W, w);
    check("next event pairs correctly after the error (l=" & integer'image(l)
          & " w=" & integer'image(w) & ")", l = 7 and w = 8);

    ---------------------------------------------------------------------------
    report "=== Test: overflow flagged, never backpressured ===";
    axi_write(R_CTRL, 2);
    for i in 1 to FIFO_DEPTH + 4 loop
      send_event(i, i * 2, i);
    end loop;
    axi_read(R_STATUS, rd);
    ok_v := ((rd / 4) mod 2) = 1;
    ok_v := ok_v and (((rd / 2) mod 2) = 1);
    check("overflow flagged after " & integer'image(FIFO_DEPTH + 4) & " undrained events", ok_v);
    check("tready never deasserted", not tready_ever_low);

    ---------------------------------------------------------------------------
    report "=== Test: clear resets everything ===";
    axi_write(R_CTRL, 2);
    axi_read(R_STATUS, rd);
    ok_v := ((rd mod 2) = 1) and (((rd / 4) mod 2) = 0) and (((rd / 8) mod 2) = 0);
    axi_read(R_COUNT, rd);
    ok_v := ok_v and (rd = 0);
    check("clear empties FIFO and clears overflow, framing error and count", ok_v);

    ---------------------------------------------------------------------------
    report "=== Test: watermark interrupt ===";
    axi_write(R_WMARK, 3);
    send_event(1, 1, 1);
    send_event(2, 2, 2);
    ok_v := (irq_o = '0');
    send_event(3, 3, 3);
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
