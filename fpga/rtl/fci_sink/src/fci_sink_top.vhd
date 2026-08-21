-- Buffered AXI4-Lite result window for fci_core.
--
-- Why this exists
-- ---------------
-- fci_core's results reached MicroBlaze through axi_dma_0, which costs ~1200 LUTs and 2 BRAM tiles
-- to move 8 bytes per event. This block replaces that path: it captures the two-beat result stream
-- into a 32-deep FIFO and presents the head over AXI4-Lite, so fci_core presents a BUFFERED
-- AXI4-Lite result output and the design keeps exactly one DMA channel -- the raw restored trace.
--
-- The buffering has to live in RTL. Vitis HLS can expose scalar outputs on an s_axilite bundle,
-- but those are single registers valid at ap_done with no queue behind them: at the 15 kcps design
-- target that reintroduces a hard 66.7 us deadline on every event, where one late interrupt loses
-- a result with nothing to show for it. A FIFO turns that deadline into ~2.1 ms of slack and lets
-- firmware drain in batches on a watermark. So fci_core keeps its AXI4-Stream output internally
-- and this block is what makes it look like a buffered register interface.
--
-- Packaging note: this is a separate IP so that fci_core's HLS output does not have to be unpacked
-- and re-instantiated. If a single block design cell is preferred over two, the pair can be
-- packaged together later -- the RTL here does not change either way.
--
-- Timestamp
-- ---------
-- The 64-bit tag fci_core forwards from the input frame's TUSER travels with each result, so an
-- FCI result can be matched to the psd_core result computed from the same pulse. That pairing
-- cannot rely on output ordering once both cores buffer independently.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.fci_sink_pkg.all;

entity fci_sink_top is
  generic (
    ACC_WIDTH  : integer := 32;
    FIFO_DEPTH : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- AXI4-Stream slave, matching fci_core's m_axis_result (32-bit data, 64-bit user timestamp).
    -- tready is tied high: fci_core is the only producer and this block must never be the reason
    -- it stalls, since a stalled fci_core backpressures the lockstep broadcaster and freezes the
    -- whole acquisition chain (project log, section 4.3).
    s_axis_tdata  : in  std_logic_vector(ACC_WIDTH - 1 downto 0);
    s_axis_tuser  : in  std_logic_vector(63 downto 0);
    s_axis_tlast  : in  std_logic;
    s_axis_tvalid : in  std_logic;
    s_axis_tready : out std_logic;

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

    irq_o : out std_logic
  );
end entity fci_sink_top;

architecture rtl of fci_sink_top is

  constant LEVEL_WIDTH : integer := clog2(FIFO_DEPTH) + 1;
  constant REC_WIDTH   : integer := 64 + 2 * ACC_WIDTH;

  signal result_valid  : std_logic;
  signal psa_l         : std_logic_vector(ACC_WIDTH - 1 downto 0);
  signal psa_w         : std_logic_vector(ACC_WIDTH - 1 downto 0);
  signal timestamp     : std_logic_vector(63 downto 0);
  signal framing_error : std_logic;

  signal fifo_din  : std_logic_vector(REC_WIDTH - 1 downto 0);
  signal fifo_dout : std_logic_vector(REC_WIDTH - 1 downto 0);
  signal fifo_empty, fifo_full, fifo_overflow : std_logic;
  signal fifo_level : std_logic_vector(LEVEL_WIDTH - 1 downto 0);

  signal watermark    : std_logic_vector(LEVEL_WIDTH - 1 downto 0);
  signal pop_strobe   : std_logic;
  signal clear_strobe : std_logic;

  signal event_count : unsigned(31 downto 0);

begin

  s_axis_tready <= '1';

  u_collector : entity work.beat_pair_collector
    generic map (ACC_WIDTH => ACC_WIDTH)
    port map (
      clk_i           => clk_i,
      rstn_i          => rstn_i,
      clear_i         => clear_strobe,
      s_valid_i       => s_axis_tvalid,
      s_data_i        => s_axis_tdata,
      s_user_i        => s_axis_tuser,
      s_last_i        => s_axis_tlast,
      result_valid_o  => result_valid,
      psa_l_o         => psa_l,
      psa_w_o         => psa_w,
      timestamp_o     => timestamp,
      framing_error_o => framing_error
    );

  fifo_din <= timestamp & psa_w & psa_l;

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

  u_regs : entity work.fci_sink_axi4lite_regs
    generic map (
      C_ADDR_WIDTH => 5,
      ACC_WIDTH    => ACC_WIDTH,
      LEVEL_WIDTH  => LEVEL_WIDTH
    )
    port map (
      clk_i           => clk_i,
      rstn_i          => rstn_i,
      s_axi_awaddr    => s_axi_awaddr,
      s_axi_awvalid   => s_axi_awvalid,
      s_axi_awready   => s_axi_awready,
      s_axi_wdata     => s_axi_wdata,
      s_axi_wstrb     => s_axi_wstrb,
      s_axi_wvalid    => s_axi_wvalid,
      s_axi_wready    => s_axi_wready,
      s_axi_bresp     => s_axi_bresp,
      s_axi_bvalid    => s_axi_bvalid,
      s_axi_bready    => s_axi_bready,
      s_axi_araddr    => s_axi_araddr,
      s_axi_arvalid   => s_axi_arvalid,
      s_axi_arready   => s_axi_arready,
      s_axi_rdata     => s_axi_rdata,
      s_axi_rresp     => s_axi_rresp,
      s_axi_rvalid    => s_axi_rvalid,
      s_axi_rready    => s_axi_rready,
      watermark_o     => watermark,
      pop_o           => pop_strobe,
      clear_o         => clear_strobe,
      psa_l_i         => fifo_dout(ACC_WIDTH - 1 downto 0),
      psa_w_i         => fifo_dout(2 * ACC_WIDTH - 1 downto ACC_WIDTH),
      timestamp_i     => fifo_dout(REC_WIDTH - 1 downto 2 * ACC_WIDTH),
      event_count_i   => std_logic_vector(event_count),
      empty_i         => fifo_empty,
      full_i          => fifo_full,
      overflow_i      => fifo_overflow,
      framing_error_i => framing_error,
      level_i         => fifo_level
    );

end architecture rtl;
