-- Top level: cross-level trigger with pre-trigger delay-line lookback and a triggered-capture
-- AXI4-Stream output, sized/formatted to wire directly into fci_core's s_axis_data port.
-- See fpga/rtl/trigger_core (project plan) for the full architecture rationale.
library ieee;
use ieee.std_logic_1164.all;
use work.trigger_core_pkg.all;

entity trigger_core_top is
  generic (
    ADC_WIDTH  : integer := 14; -- matches this board's LTC2248
    MAX_DELAY  : integer := 256;
    MAX_DEPTH  : integer := 4096
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- Plain ADC input, offset binary. "Make External" at the block-design level.
    adc_data_i : in std_logic_vector(ADC_WIDTH - 1 downto 0);

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
    m_axis_tuser  : out std_logic_vector(0 downto 0);
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

  signal delayed_data : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal trigger_pulse : std_logic;
  signal armed          : std_logic;

  signal buf_wr_en   : std_logic;
  signal buf_wr_addr  : std_logic_vector(ADDR_WIDTH - 1 downto 0);
  signal buf_wr_data   : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal buf_rd_en    : std_logic;
  signal buf_rd_addr   : std_logic_vector(ADDR_WIDTH - 1 downto 0);
  signal buf_rd_data    : std_logic_vector(ADC_WIDTH - 1 downto 0);

begin

  -- AXI-Stream side-channel bytes/sideband: fixed, not meaningful for this simple single-stream
  -- design, but driven to match fci_core's port signature exactly for direct wiring.
  m_axis_tkeep <= (others => '1');
  m_axis_tstrb <= (others => '1');
  m_axis_tuser <= (others => '0');
  m_axis_tid   <= (others => '0');
  m_axis_tdest <= (others => '0');

  u_axi4lite_regs : entity work.axi4lite_regs
    generic map (
      C_ADDR_WIDTH => 5
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
      data_i      => adc_data_i,
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
      adc_data_i  => adc_data_i,
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
      DEPTH      => MAX_DEPTH
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
