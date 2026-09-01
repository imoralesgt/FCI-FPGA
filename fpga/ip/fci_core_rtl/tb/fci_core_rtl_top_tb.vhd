-- Integration testbench for the assembled fci_core_rtl_top: sample_framer + the real Xilinx FFT IP
-- + bin_accumulator + result_fifo + fci_axi4lite_regs, driven with REAL detector traces.
--
-- Why real traces rather than a synthetic tone
-- -------------------------------------------
-- A single-tone stimulus verifies that energy lands in the right bin, but it cannot distinguish a
-- correct implementation from one with a subtly wrong bin mapping: a tone is symmetric and sparse,
-- so a swapped Re/Im half or a partially-wrong bit reversal can still put a peak "somewhere
-- plausible". Real pulses have broadband, asymmetric spectra where any such error changes the
-- window sums immediately and unmistakably. These 2048-sample traces were captured on hardware
-- under a DD neutron generator on 2026-08-28 at exactly the transform length this core now uses,
-- so they need no resampling to be valid stimulus (see tb/gen_golden.py).
--
-- What is checked, and why it is the RATIO
-- ----------------------------------------
-- The FFT runs in block floating point: each frame is scaled by its own exponent, chosen from that
-- frame's magnitude. fci_core_rtl_top deliberately discards that exponent (see its m_axis_status
-- tie-off) because it cancels in psa_l/psa_w, which is the only quantity firmware derives from
-- these registers. So the absolute sums legitimately differ from a float reference by an arbitrary
-- per-frame power of two -- measured at 2^8 for strong pulses, 2^4 for weak ones -- while the
-- ratio is scale-invariant. Checking absolute values here would fail on correct hardware; checking
-- the ratio is both meaningful and still strict enough to catch the failure this test exists for
-- (a wrong bin mapping moves the ratio by tens of percent).
--
-- TOL_FRAC is a structural-correctness bound, NOT a precision specification. A wrong bin mapping,
-- a swapped Re/Im half, or a broken bit reversal moves the ratio by tens of percent; 5% is far
-- inside that while leaving margin over the quantization actually measured here.
--
-- Measured agreement against the float reference, and a trend worth knowing: it is 0.24-1.09% for
-- the Li-6-capture-peak energies this instrument is characterized on (~2.4e6-2.9e6), but degrades
-- with INCREASING energy -- 2.98% at 4.65e6. That is block floating point doing its job: a
-- higher-amplitude frame is downscaled harder to avoid overflow, so fewer significant bits survive
-- into the 16-bit output. If on-hardware FCI ever proves precision-limited at high energy, the
-- lever is the FFT's input_width/output_width (both, together -- BFP requires them equal), at a
-- resource cost this device may not have room for.
--
-- Requires the generated xfft_2048 IP -- run scripts/generate_ip.tcl first, then this via
-- scripts/run_sim_top.tcl (a Vivado project flow: the plain xvhdl/xelab path cannot elaborate the
-- IP's simulation model).
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
use std.textio.all;

entity fci_core_rtl_top_tb is
end entity fci_core_rtl_top_tb;

architecture sim of fci_core_rtl_top_tb is

  constant FFT_LENGTH : integer := 2048;
  constant DATA_WIDTH : integer := 16;
  constant ACC_WIDTH  : integer := 32;
  constant N_TRACES   : integer := 8;
  constant CLK_PERIOD : time := 20 ns; -- 50 MHz, the real fabric clock for this datapath

  -- Windows the TB programs; must match gen_golden.py's PSA_L_*/PSA_W_*.
  constant PSA_L_LO : integer := 1;
  constant PSA_L_HI : integer := 10;
  constant PSA_W_LO : integer := 1;
  constant PSA_W_HI : integer := 40;

  constant TOL_FRAC : real := 0.05; -- structural bound, not a precision spec -- see header

  -- Register map (fci_axi4lite_regs.vhd)
  constant ADDR_PSA_L_LO  : integer := 16#00#;
  constant ADDR_PSA_L_HI  : integer := 16#04#;
  constant ADDR_PSA_W_LO  : integer := 16#08#;
  constant ADDR_PSA_W_HI  : integer := 16#0C#;
  constant ADDR_CTRL      : integer := 16#10#;
  constant ADDR_STATUS    : integer := 16#14#;
  constant ADDR_PSA_L     : integer := 16#18#;
  constant ADDR_PSA_W     : integer := 16#1C#;
  constant ADDR_EVENT_CNT : integer := 16#28#;

  constant CTRL_POP : integer := 1;

  signal clk  : std_logic := '0';
  signal rstn : std_logic := '0';

  -- Stops the clock once the stimulus is finished. Without this the concurrent clock assignment
  -- schedules an event every half period forever, so "run all" never returns and the xsim kernel
  -- spins at 100% CPU indefinitely after the pass/fail summary has already been reported.
  signal sim_done : std_logic := '0';

  signal s_axis_tdata  : std_logic_vector(DATA_WIDTH - 1 downto 0) := (others => '0');
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

  signal irq : std_logic;

  signal test_count : integer := 0;
  signal fail_count : integer := 0;

  procedure check(msg : in string; ok : in boolean;
                  signal tc : inout integer; signal fc : inout integer) is
  begin
    tc <= tc + 1;
    if ok then
      report "  PASS: " & msg;
    else
      report "  FAIL: " & msg severity error;
      fc <= fc + 1;
    end if;
  end procedure check;

begin

  clk <= not clk after CLK_PERIOD / 2 when sim_done = '0' else '0';

  dut : entity work.fci_core_rtl_top
    generic map (
      FFT_LENGTH => FFT_LENGTH,
      DATA_WIDTH => DATA_WIDTH,
      ACC_WIDTH  => ACC_WIDTH,
      FIFO_DEPTH => 32
    )
    port map (
      clk_i         => clk,
      rstn_i        => rstn,
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
      irq_o         => irq
    );

  stim : process
    file stim_f   : text;
    file golden_f : text;
    variable l          : line;
    variable status     : file_open_status;
    variable sample     : integer;
    variable exp_fci    : real;
    variable got_fci    : real;
    variable got_l      : integer;
    variable got_w      : integer;
    variable got_cnt    : integer;
    variable good       : boolean;

    procedure axi_write(addr : in integer; data : in integer) is
    begin
      wait until rising_edge(clk);
      s_axi_awaddr  <= std_logic_vector(to_unsigned(addr, 6));
      s_axi_awvalid <= '1';
      s_axi_wdata   <= std_logic_vector(to_unsigned(data, 32));
      s_axi_wvalid  <= '1';
      wait until rising_edge(clk) and s_axi_awready = '1' and s_axi_wready = '1';
      s_axi_awvalid <= '0';
      s_axi_wvalid  <= '0';
      wait until rising_edge(clk) and s_axi_bvalid = '1';
    end procedure axi_write;

    procedure axi_read(addr : in integer; result : out integer) is
    begin
      wait until rising_edge(clk);
      s_axi_araddr  <= std_logic_vector(to_unsigned(addr, 6));
      s_axi_arvalid <= '1';
      wait until rising_edge(clk) and s_axi_arready = '1';
      s_axi_arvalid <= '0';
      wait until rising_edge(clk) and s_axi_rvalid = '1';
      result := to_integer(unsigned(s_axi_rdata));
    end procedure axi_read;

    -- Streams one 2048-sample frame from stimulus.txt, honouring tready.
    procedure drive_frame(tag : in integer) is
    begin
      for i in 0 to FFT_LENGTH - 1 loop
        -- Read the next non-comment line. The exit condition is on having FOUND a data line, not
        -- on file state: an earlier version exited on `not endfile`, which is true on virtually
        -- every iteration, so it fell through without reading and every sample came back 0.
        loop
          assert not endfile(stim_f)
            report "stimulus.txt exhausted -- regenerate with tb/gen_golden.py" severity failure;
          readline(stim_f, l);
          exit when l'length > 0 and l.all(1) /= '#';
        end loop;
        read(l, sample);

        s_axis_tdata  <= std_logic_vector(to_signed(sample, DATA_WIDTH));
        s_axis_tuser  <= std_logic_vector(to_unsigned(tag, 64));
        s_axis_tvalid <= '1';
        if i = FFT_LENGTH - 1 then
          s_axis_tlast <= '1';
        else
          s_axis_tlast <= '0';
        end if;
        wait until rising_edge(clk) and s_axis_tready = '1';
      end loop;
      s_axis_tvalid <= '0';
      s_axis_tlast  <= '0';
    end procedure drive_frame;

    -- Drives a capture of the WRONG length, using synthetic samples so the golden file stays in
    -- step. This is the power-on case that took the instrument down: trigger_core's registers reset
    -- to depth=0, which clamps to a one-beat capture, and a threshold of 0 on blr_core's
    -- zero-centred output fires within microseconds of configuration -- long before firmware writes
    -- a sane depth. Forwarding that TLAST to a 2048-point FFT raises event_tlast_unexpected and
    -- HALTS its data input channel; being lockstep, axis_broadcaster_0 then freezes psd_core and
    -- axi_dma_1 too and trigger_core never re-arms. The framer must absorb this without wedging.
    procedure drive_short_frame(beats : in integer; tag : in integer) is
    begin
      for i in 0 to beats - 1 loop
        s_axis_tdata  <= std_logic_vector(to_signed(1000 + i, DATA_WIDTH));
        s_axis_tuser  <= std_logic_vector(to_unsigned(tag, 64));
        s_axis_tvalid <= '1';
        if i = beats - 1 then
          s_axis_tlast <= '1';
        else
          s_axis_tlast <= '0';
        end if;
        wait until rising_edge(clk) and s_axis_tready = '1';
      end loop;
      s_axis_tvalid <= '0';
      s_axis_tlast  <= '0';
    end procedure drive_short_frame;

  begin
    file_open(status, stim_f, "stimulus.txt", read_mode);
    assert status = open_ok
      report "cannot open stimulus.txt -- run tb/gen_golden.py first" severity failure;
    file_open(status, golden_f, "golden.txt", read_mode);
    assert status = open_ok
      report "cannot open golden.txt -- run tb/gen_golden.py first" severity failure;

    -- Discard golden.txt's comment header.
    readline(golden_f, l);

    rstn <= '0';
    for i in 0 to 9 loop
      wait until rising_edge(clk);
    end loop;
    rstn <= '1';
    for i in 0 to 9 loop
      wait until rising_edge(clk);
    end loop;

    report "=== Programming bin windows ===";
    axi_write(ADDR_PSA_L_LO, PSA_L_LO);
    axi_write(ADDR_PSA_L_HI, PSA_L_HI);
    axi_write(ADDR_PSA_W_LO, PSA_W_LO);
    axi_write(ADDR_PSA_W_HI, PSA_W_HI);

    -- Deliberately first, in the same order hardware sees it: the malformed power-on capture
    -- arrives before any real event. If the framer let this reach the FFT as a 1-beat frame, the
    -- IP would halt here and EVERY check below would fail -- which is exactly what the instrument
    -- did on silicon while this testbench, which only ever drove well-formed frames, passed.
    report "=== Malformed capture: 1 beat against a 2048-point FFT ===";
    drive_short_frame(1, 16#0BAD#);
    for i in 0 to 20 * FFT_LENGTH loop
      wait until rising_edge(clk);
      exit when irq = '1';
    end loop;
    axi_read(ADDR_PSA_W, got_w);
    -- The frame is zero-padded, so bin 0 carries the single sample and psa_w must be non-zero.
    -- Any result at all proves the FFT accepted a complete frame instead of halting.
    check("short capture is padded to a full frame, not dropped or halted (psa_w = "
          & integer'image(got_w) & ")", got_w /= 0, test_count, fail_count);
    axi_write(ADDR_CTRL, CTRL_POP);

    report "=== Streaming " & integer'image(N_TRACES) & " real 2048-sample traces ===";
    for t in 0 to N_TRACES - 1 loop
      drive_frame(16#1000# + t); -- a distinguishable timestamp per frame

      readline(golden_f, l);
      read(l, exp_fci);

      -- Wait for this frame's result to reach the FIFO (FFT latency + accumulation).
      for i in 0 to 20 * FFT_LENGTH loop
        wait until rising_edge(clk);
        exit when irq = '1';
      end loop;

      axi_read(ADDR_PSA_L, got_l);
      axi_read(ADDR_PSA_W, got_w);

      -- A zero psa_w would mean the wide window accumulated nothing at all -- a real failure, and
      -- one that must not reach the division below.
      check("trace " & integer'image(t) & " psa_w is non-zero (got "
            & integer'image(got_w) & ")", got_w /= 0, test_count, fail_count);

      if got_w /= 0 then
        got_fci := real(got_l) / real(got_w);
        good := abs(got_fci - exp_fci) <= TOL_FRAC * exp_fci;
        check("trace " & integer'image(t) & " FCI = " & real'image(got_fci)
              & " (expected ~" & real'image(exp_fci) & ", psa_l=" & integer'image(got_l)
              & " psa_w=" & integer'image(got_w) & ")", good, test_count, fail_count);
      end if;

      axi_write(ADDR_CTRL, CTRL_POP);
    end loop;

    -- N_TRACES real traces plus the malformed one driven first. The padded short capture counts as
    -- a genuine accumulated frame and SHOULD be counted: it produced a result the firmware can pop,
    -- so hiding it from the event counter would desynchronise the count from the FIFO contents.
    report "=== Event counter tracks every frame accumulated ===";
    axi_read(ADDR_EVENT_CNT, got_cnt);
    check("event_count = " & integer'image(got_cnt) & " (expected "
          & integer'image(N_TRACES + 1) & ", including the padded short capture)",
          got_cnt = N_TRACES + 1, test_count, fail_count);

    wait for CLK_PERIOD;
    report "=== " & integer'image(test_count) & " tests run, "
           & integer'image(fail_count) & " failed ===";
    if fail_count = 0 then
      report "TEST PASSED";
    else
      report "TEST FAILED" severity error;
    end if;

    file_close(stim_f);
    file_close(golden_f);
    sim_done <= '1';
    wait;
  end process stim;

end architecture sim;
