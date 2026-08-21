-- Top level: CAEN-style pulse-shape discrimination front-end.
--
-- Consumes the framed, baseline-restored trace that trigger_core broadcasts, integrates it over a
-- short and a long gate, and buffers {ENERGY_SHORT, ENERGY, timestamp} for MicroBlaze to read over
-- AXI4-Lite. The ratio ENERGY_SHORT/ENERGY is the discrimination parameter, computed on the CPU --
-- the same division-on-the-host split fci_core already uses, and for the same reason.
--
-- Never backpressures
-- -------------------
-- s_axis_tready is tied high, permanently. This is not laziness: axis_broadcaster_0 is LOCKSTEP,
-- so no beat advances unless every master accepts it. A psd_core that stalled would stall
-- fci_core and the raw-trace DMA with it, and the project has already lost a debugging campaign to
-- exactly that failure mode (see the project log, section 4.3, where a one-shot axi_dma_0 froze the
-- whole pipeline through this same broadcaster). The only place a result can be lost is a full
-- result FIFO, and that is counted and reported rather than absorbed silently.
--
-- Timestamp
-- ---------
-- trigger_core tags each frame with a free-running 64-bit cycle count on TUSER, held constant for
-- every beat of the frame. This core latches it on the frame's first beat and emits it alongside
-- the integrals, so a PSD result can be matched to the fci_core result computed from the same
-- pulse. In-band tagging rather than a shared counter register is what makes that pairing survive
-- the two cores buffering their results independently: once each has its own FIFO, position in the
-- output sequence no longer implies a common event.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.psd_core_pkg.all;

entity psd_core_top is
  generic (
    DATA_WIDTH : integer := 16;
    MAX_DEPTH  : integer := 4096;
    ACC_WIDTH  : integer := 32;
    FIFO_DEPTH : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- AXI4-Stream slave, matching trigger_core's master (16-bit data, 64-bit user timestamp).
    s_axis_tdata  : in  std_logic_vector(15 downto 0);
    s_axis_tuser  : in  std_logic_vector(63 downto 0);
    s_axis_tlast  : in  std_logic;
    s_axis_tvalid : in  std_logic;
    s_axis_tready : out std_logic;

    -- AXI4-Lite slave; see psd_axi4lite_regs for the map.
    s_axi_awaddr  : in  std_logic_vector(5 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_araddr  : in  std_logic_vector(5 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;

    irq_o : out std_logic
  );
end entity psd_core_top;

architecture rtl of psd_core_top is

  constant IDX_WIDTH   : integer := clog2(MAX_DEPTH);
  constant LEVEL_WIDTH : integer := clog2(FIFO_DEPTH) + 1;
  constant REC_WIDTH   : integer := 64 + 2 * ACC_WIDTH;

  signal pre_trigger  : std_logic_vector(IDX_WIDTH - 1 downto 0);
  signal pre_gate     : std_logic_vector(IDX_WIDTH - 1 downto 0);
  signal short_gate   : std_logic_vector(IDX_WIDTH - 1 downto 0);
  signal long_gate    : std_logic_vector(IDX_WIDTH - 1 downto 0);
  signal baseline_ref : std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal watermark    : std_logic_vector(LEVEL_WIDTH - 1 downto 0);

  signal pop_strobe   : std_logic;
  signal clear_strobe : std_logic;

  signal result_valid : std_logic;
  signal energy_short : std_logic_vector(ACC_WIDTH - 1 downto 0);
  signal energy_long  : std_logic_vector(ACC_WIDTH - 1 downto 0);

  signal frame_start : std_logic;
  signal ts_latched  : std_logic_vector(63 downto 0);

  signal fifo_din  : std_logic_vector(REC_WIDTH - 1 downto 0);
  signal fifo_dout : std_logic_vector(REC_WIDTH - 1 downto 0);
  signal fifo_empty, fifo_full, fifo_overflow : std_logic;
  signal fifo_level : std_logic_vector(LEVEL_WIDTH - 1 downto 0);

  signal event_count : unsigned(31 downto 0);

begin

  -- See header: tying this high is a hard requirement of the lockstep broadcaster, not a shortcut.
  s_axis_tready <= '1';

  -- Latch the frame's timestamp on its first beat. frame_start is armed by the previous frame's
  -- tlast, so it tracks frame boundaries without needing the integrator's internal index.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        frame_start <= '1';
        ts_latched  <= (others => '0');
      elsif s_axis_tvalid = '1' then
        if frame_start = '1' then
          ts_latched <= s_axis_tuser;
        end if;
        frame_start <= s_axis_tlast;
      end if;
    end if;
  end process;

  u_integrator : entity work.dual_gate_integrator
    generic map (
      DATA_WIDTH => DATA_WIDTH,
      MAX_DEPTH  => MAX_DEPTH,
      ACC_WIDTH  => ACC_WIDTH
    )
    port map (
      clk_i          => clk_i,
      rstn_i         => rstn_i,
      s_valid_i      => s_axis_tvalid,
      s_data_i       => s_axis_tdata(DATA_WIDTH - 1 downto 0),
      s_last_i       => s_axis_tlast,
      baseline_ref_i => baseline_ref,
      pre_trigger_i  => pre_trigger,
      pre_gate_i     => pre_gate,
      short_gate_i   => short_gate,
      long_gate_i    => long_gate,
      result_valid_o => result_valid,
      energy_short_o => energy_short,
      energy_long_o  => energy_long
    );

  fifo_din <= ts_latched & energy_long & energy_short;

  u_fifo : entity work.result_fifo
    generic map (
      REC_WIDTH => REC_WIDTH,
      DEPTH     => FIFO_DEPTH
    )
    port map (
      clk_i      => clk_i,
      rstn_i     => rstn_i,
      push_i     => result_valid,
      data_i     => fifo_din,
      pop_i      => pop_strobe,
      data_o     => fifo_dout,
      empty_o    => fifo_empty,
      full_o     => fifo_full,
      level_o    => fifo_level,
      overflow_o => fifo_overflow,
      clear_i    => clear_strobe
    );

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        event_count <= (others => '0');
      elsif clear_strobe = '1' then
        event_count <= (others => '0');
      elsif result_valid = '1' then
        event_count <= event_count + 1;
      end if;
    end if;
  end process;

  -- Watermark interrupt: level-sensitive, so it stays asserted until firmware drains below the
  -- mark. A watermark of 0 disables it, which is how a polled bring-up run turns interrupts off
  -- without touching the interrupt controller.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        irq_o <= '0';
      elsif unsigned(watermark) = 0 then
        irq_o <= '0';
      elsif unsigned(fifo_level) >= unsigned(watermark) then
        irq_o <= '1';
      else
        irq_o <= '0';
      end if;
    end if;
  end process;

  u_regs : entity work.psd_axi4lite_regs
    generic map (
      C_ADDR_WIDTH => 6,
      DATA_WIDTH   => DATA_WIDTH,
      IDX_WIDTH    => IDX_WIDTH,
      ACC_WIDTH    => ACC_WIDTH,
      LEVEL_WIDTH  => LEVEL_WIDTH
    )
    port map (
      clk_i          => clk_i,
      rstn_i         => rstn_i,
      s_axi_awaddr   => s_axi_awaddr,
      s_axi_awvalid  => s_axi_awvalid,
      s_axi_awready  => s_axi_awready,
      s_axi_wdata    => s_axi_wdata,
      s_axi_wstrb    => s_axi_wstrb,
      s_axi_wvalid   => s_axi_wvalid,
      s_axi_wready   => s_axi_wready,
      s_axi_bresp    => s_axi_bresp,
      s_axi_bvalid   => s_axi_bvalid,
      s_axi_bready   => s_axi_bready,
      s_axi_araddr   => s_axi_araddr,
      s_axi_arvalid  => s_axi_arvalid,
      s_axi_arready  => s_axi_arready,
      s_axi_rdata    => s_axi_rdata,
      s_axi_rresp    => s_axi_rresp,
      s_axi_rvalid   => s_axi_rvalid,
      s_axi_rready   => s_axi_rready,
      pre_trigger_o  => pre_trigger,
      pre_gate_o     => pre_gate,
      short_gate_o   => short_gate,
      long_gate_o    => long_gate,
      baseline_ref_o => baseline_ref,
      watermark_o    => watermark,
      pop_o          => pop_strobe,
      clear_o        => clear_strobe,
      energy_short_i => fifo_dout(ACC_WIDTH - 1 downto 0),
      energy_long_i  => fifo_dout(2 * ACC_WIDTH - 1 downto ACC_WIDTH),
      timestamp_i    => fifo_dout(REC_WIDTH - 1 downto 2 * ACC_WIDTH),
      event_count_i  => std_logic_vector(event_count),
      empty_i        => fifo_empty,
      full_i         => fifo_full,
      overflow_i     => fifo_overflow,
      level_i        => fifo_level
    );

end architecture rtl;
