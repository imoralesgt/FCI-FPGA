-- Top level: cross-level trigger with pre-trigger delay-line lookback and a triggered-capture
-- AXI4-Stream output, sized/formatted to wire directly into fci_core's s_axis_data port.
-- See fpga/rtl/trigger_core (project plan) for the full architecture rationale.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity trigger_core_top is
  generic (
    -- Width of the SAMPLE datapath, not of the ADC. blr_core upstream subtracts a signed baseline
    -- from a signed 14-bit converter word, so its output spans +/-16383 and needs 15 bits; 16 is
    -- the byte-multiple AXI4-Stream wants and is what blr_core emits. Truncating back to 14 here
    -- would silently clip the largest excursions -- exactly the pulses that matter most.
    ADC_WIDTH  : integer := 16;
    -- Retained only for a standalone build fed directly from an offset-binary converter. With
    -- blr_core upstream the stream is already signed and this must be false -- but note the whole
    -- offset-binary representation is gone from the normal chain now, so there is no longer a
    -- double-conversion hazard to trip over, just a format this core can still accept.
    ADC_IS_2C  : boolean := false;
    MAX_DELAY  : integer := 256;
    MAX_DEPTH  : integer := 4096
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- Sample stream in, from blr_core. AXI4-Stream for CONNECTIVITY only: s_axis_tready is
    -- driven permanently high, because this is a continuous 50 Msps converter stream with no
    -- buffer behind it -- a beat not taken is a sample destroyed, so backpressure here would
    -- corrupt the time base rather than delay it. blr_core's matching master ignores tready for
    -- the same reason. TDATA is 16 bits with the ADC_WIDTH sample in the low bits.
    s_axis_tdata  : in  std_logic_vector(15 downto 0);
    s_axis_tvalid : in  std_logic;
    s_axis_tready : out std_logic;

    -- AXI4-Lite slave: threshold (0x00), polarity (0x04), delay (0x08), depth (0x0C).
    s_axi_awaddr  : in  std_logic_vector(4 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_araddr  : in  std_logic_vector(4 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;

    -- AXI4-Stream master, sized to match fci_core's s_axis_data exactly (ap_axiu<16,1,1,1>).
    m_axis_tdata  : out std_logic_vector(15 downto 0);
    m_axis_tkeep  : out std_logic_vector(1 downto 0);
    m_axis_tstrb  : out std_logic_vector(1 downto 0);
    -- 64-bit event timestamp, held constant across every beat of a frame. See the timestamp
    -- comment in the architecture body for why it travels in-band rather than in a register.
    m_axis_tuser  : out std_logic_vector(63 downto 0);
    m_axis_tlast  : out std_logic;
    m_axis_tid    : out std_logic_vector(0 downto 0);
    m_axis_tdest  : out std_logic_vector(0 downto 0);
    m_axis_tvalid : out std_logic;
    m_axis_tready : in  std_logic
  );
end entity trigger_core_top;

architecture rtl of trigger_core_top is

  constant ADDR_WIDTH : integer := clog2(MAX_DEPTH - 1);

  signal threshold : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal polarity  : std_logic;
  signal delay_sel : std_logic_vector(clog2(MAX_DELAY) - 1 downto 0);
  signal depth     : std_logic_vector(clog2(MAX_DEPTH) - 1 downto 0);

  -- Raw ADC bus, registered with NO combinational logic in front of this flop -- confirmed against
  -- the sibling gamma-spectroscopy project (same board/ADC, same clk_adc/clk_dpp-equivalent
  -- clocking structure, and known to run clean even near detector saturation): its own first touch
  -- of adc_data is exactly this pattern, `samples <= signed(adc_data) & ...`, straight into a
  -- flop before any processing. All 14 bits get identical, symmetric pin-to-register timing this
  -- way -- no bit is treated differently, unlike putting the MSB-flip conversion below ahead of
  -- this register (which would have given the MSB an extra LUT relative to the other 13 bits,
  -- right at the one timing-critical moment where any asymmetry matters most, given how thin the
  -- margin on this unconstrained bus turned out to be in practice).
  signal adc_data_q : std_logic_vector(ADC_WIDTH - 1 downto 0);

  -- NOTE: this register used to carry an `attribute IOB` forcing it into the I/O blocks, because
  -- the input was wired straight to the ADC pins. blr_core now sits between the pins and this
  -- core, so this is a fabric-to-fabric register and the attribute has no pad to target; it moved
  -- to blr_core_top along with ownership of the pins. The measurements behind it, and the two
  -- disproofs of the bus-skew hypothesis it was once wrongly credited with fixing, are in the
  -- project log (section 5).

  -- The LTC2248's MODE pin is strapped to 2/3 VDD on this board, which per its datasheet selects
  -- 2's-complement output format, not the offset-binary format the rest of this core (trigger.vhd's
  -- unsigned comparator, and fci_core downstream) assumes. The two formats differ by exactly the
  -- MSB, so converting is a single bit inversion -- done here on the already-registered adc_data_q
  -- (not on the raw port -- see above), so every comparison/delay/capture downstream operates on
  -- offset-binary data that's one full cycle removed from the timing-critical capture moment. This
  -- is board-specific integration knowledge, kept here rather than in trigger.vhd/delay_line.vhd
  -- (which stay format-agnostic, "offset binary in"), matching this file's existing role as the
  -- board-aware top level (ADC_WIDTH's default already documents "matches this board's LTC2248").
  --
  -- THIS IS THE FIX for the amplitude-dependent pulse distortion that dominated bring-up (a fast
  -- overshoot spike, a flat plateau, an undershoot, then a slow settle, on large pulses only, with
  -- small ones passing intact). Reading 2's complement as unsigned maps analog value V to U = V for
  -- V >= 0 and V + 16384 for V < 0: monotonic everywhere EXCEPT across analog zero, where U folds
  -- from 16383 straight to 0. The baseline sits ~6300 counts below analog zero (offset-binary
  -- ~1861; the same baseline read raw is 1861 xor 8192 = 10053, which is the "baseline close to
  -- 10000" recorded in the early bring-up logs). Pulses smaller than that gap never reach the fold;
  -- larger ones cross it and cliff, and if they also over-range they clip flat at the rail on the
  -- far side before cliffing back on the way down.
  --
  -- Reproduced on hardware 2026-08-18 by rendering one capture both ways (firmware's
  -- test_encoding_fold_demo(), gain raised until a pulse crossed analog zero):
  --     idx  corrected  as-read-before-fix
  --      97     6335        14527
  --      98     8935          743   <- crosses analog zero, folds 16383->0
  --     101    16383         8191   <- clipped at the rail
  --     ...    16383         8191      (85 samples of flat plateau)
  --     232     8225           33   <- decayed to near zero: the "undershoot"
  --     233     8163        16355   <- folds back, then settles slowly to baseline
  -- The corrected column over those same samples is a clean pulse.
  signal adc_data_ob : std_logic_vector(ADC_WIDTH - 1 downto 0);

  signal ts_counter : unsigned(63 downto 0); -- free-running cycle count, the time reference
  signal ts_latched : unsigned(63 downto 0); -- value at the trigger, held for the whole frame

  signal delayed_data : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal trigger_pulse : std_logic;
  signal armed          : std_logic;

  signal buf_wr_en   : std_logic;
  -- One bit wider than the capture depth needs: capture_engine is double-buffered and uses
  -- the top bit to select which half it is addressing.
  signal buf_wr_addr  : std_logic_vector(ADDR_WIDTH downto 0);
  signal buf_wr_data   : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal buf_rd_en    : std_logic;
  signal buf_rd_addr   : std_logic_vector(ADDR_WIDTH downto 0);
  signal buf_rd_data    : std_logic_vector(ADC_WIDTH - 1 downto 0);

begin

  -- AXI-Stream side-channel bytes/sideband: fixed, not meaningful for this simple single-stream
  -- design, but driven to match fci_core's port signature exactly for direct wiring.
  -- See the port comment: never backpressure a free-running converter stream.
  s_axis_tready <= '1';

  m_axis_tkeep <= (others => '1');
  m_axis_tstrb <= (others => '1');
  -- Event timestamp. A free-running cycle counter is latched the moment the trigger fires and
  -- held on TUSER for every beat of the resulting frame, so each consumer -- psd_core, fci_core,
  -- the shaper -- can tag its own result with the pulse it came from. This has to be in-band
  -- rather than a counter register the CPU reads, because each consumer buffers its results
  -- independently: once psd_core has a result FIFO, position in the output sequence no longer
  -- implies a common event, and only a tag carried with the data still pairs them.
  --
  -- The counter is free-running from reset and is never gated: it is the time reference, so a
  -- stall would silently distort every interval derived from it. At 50 MHz, 64 bits wraps after
  -- ~11,700 years, so wrap handling is not a case the firmware has to carry.
  --
  -- Latching at the trigger (not at the start of streaming) is what makes the value mean "when the
  -- pulse crossed threshold" rather than "when the core got round to draining it". Holding it
  -- until the next trigger is safe because this core is single-buffered: armed_o is only high in
  -- IDLE, so exactly one frame is ever in flight.
  m_axis_tuser <= std_logic_vector(ts_latched);
  m_axis_tid   <= (others => '0');
  m_axis_tdest <= (others => '0');

  -- Deliberately no reset: an IOB input flop must be a plain clock-enabled/unconditioned register
  -- for the packer to place it in the I/O block, and a reset here would buy nothing anyway -- this
  -- is a pure data pipeline stage, overwritten from the pins every single cycle, and everything
  -- downstream (delay_line, trigger, capture_engine) carries its own reset.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if s_axis_tvalid = '1' then
        adc_data_q <= s_axis_tdata(ADC_WIDTH - 1 downto 0);
      end if;
    end if;
  end process;

  gen_2c : if ADC_IS_2C generate
    adc_data_ob <= (not adc_data_q(ADC_WIDTH - 1)) & adc_data_q(ADC_WIDTH - 2 downto 0);
  end generate gen_2c;

  -- Already offset binary (blr_core converted it upstream): pass through untouched.
  gen_ob : if not ADC_IS_2C generate
    adc_data_ob <= adc_data_q;
  end generate gen_ob;

  timestamp_counter : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        ts_counter <= (others => '0');
        ts_latched <= (others => '0');
      else
        ts_counter <= ts_counter + 1;
        if trigger_pulse = '1' then
          ts_latched <= ts_counter;
        end if;
      end if;
    end if;
  end process timestamp_counter;

  u_axi4lite_regs : entity work.axi4lite_regs
    generic map (
      C_ADDR_WIDTH => 5,
      DATA_WIDTH   => ADC_WIDTH,
      -- Must track MAX_DEPTH: capture_engine's depth_i is clog2(MAX_DEPTH) wide, and this used to
      -- be hardcoded to 13 inside the register file, which pinned the whole core to MAX_DEPTH=4096.
      DEPTH_BITS   => clog2(MAX_DEPTH)
    )
    port map (
      clk_i         => clk_i,
      rstn_i        => rstn_i,
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
      threshold_o   => threshold,
      polarity_o    => polarity,
      delay_o       => delay_sel,
      depth_o       => depth
    );

  u_delay_line : entity work.delay_line
    generic map (
      DATA_WIDTH => ADC_WIDTH,
      MAX_DELAY  => MAX_DELAY
    )
    port map (
      clk_i       => clk_i,
      rstn_i      => rstn_i,
      delay_sel_i => delay_sel,
      data_i      => adc_data_ob,
      data_o      => delayed_data
    );

  u_trigger : entity work.trigger
    generic map (
      DATA_WIDTH => ADC_WIDTH
    )
    port map (
      clk_i       => clk_i,
      rstn_i      => rstn_i,
      armed_i     => armed,
      adc_data_i  => adc_data_ob,
      threshold_i => threshold,
      polarity_i  => polarity,
      trigger_o   => trigger_pulse
    );

  u_capture_engine : entity work.capture_engine
    generic map (
      DATA_WIDTH => ADC_WIDTH,
      MAX_DEPTH  => MAX_DEPTH
    )
    port map (
      clk_i           => clk_i,
      rstn_i          => rstn_i,
      depth_i         => depth,
      trigger_i       => trigger_pulse,
      armed_o         => armed,
      delayed_data_i  => delayed_data,
      buf_wr_en_o     => buf_wr_en,
      buf_wr_addr_o   => buf_wr_addr,
      buf_wr_data_o   => buf_wr_data,
      buf_rd_en_o     => buf_rd_en,
      buf_rd_addr_o   => buf_rd_addr,
      buf_rd_data_i   => buf_rd_data,
      m_axis_tdata_o  => m_axis_tdata,
      m_axis_tvalid_o => m_axis_tvalid,
      m_axis_tlast_o  => m_axis_tlast,
      m_axis_tready_i => m_axis_tready
    );

  u_circular_buffer : entity work.circular_buffer
    generic map (
      DATA_WIDTH => ADC_WIDTH,
      -- Twice MAX_DEPTH: capture_engine is double-buffered and uses the top address bit as the
      -- buffer select, so both halves share this one dual-port RAM.
      DEPTH      => 2 * MAX_DEPTH
    )
    port map (
      clk_i     => clk_i,
      wr_en_i   => buf_wr_en,
      wr_addr_i => buf_wr_addr,
      wr_data_i => buf_wr_data,
      rd_en_i   => buf_rd_en,
      rd_addr_i => buf_rd_addr,
      rd_data_o => buf_rd_data
    );

end architecture rtl;
