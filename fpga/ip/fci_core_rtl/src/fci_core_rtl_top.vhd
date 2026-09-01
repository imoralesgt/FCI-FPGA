-- FCI core, hand-written VHDL: captured trace in (AXI4-Stream), buffered PSA results out
-- (AXI4-Lite). Replaces BOTH the Vitis HLS `fci_core` and the separate `fci_sink` IP.
--
-- Why this replaced the HLS core
-- ------------------------------
-- The HLS core accumulated each FFT bin's magnitude into an `ap_ufixed<18,2>` -- 16 fractional
-- bits. Combined with block-floating-point scaling, whose single per-frame exponent is set by the
-- LARGEST bin, that put the bins in psa_w's wide window (far from this detector's very low
-- spectral corner, ~bin 2-3) so far down the frame's dynamic range that quantization noise
-- dominated the real signal there. Measured on 32,544 live events: PSD resolved the Li-6 capture
-- peak at cv 1.8% while FCI on the SAME events managed cv 42% with no usable separation. No
-- runtime window retuning can fix a precision floor.
--
-- bin_accumulator.vhd fixes it structurally: 32-bit unsigned accumulators over full-width 17-bit
-- magnitudes, no fractional truncation anywhere. The scale is arbitrary but identical for both
-- windows, and FCI is their ratio, so the absolute scale never mattered -- only the precision did.
--
-- Why fci_sink is merged in rather than kept downstream
-- ----------------------------------------------------
-- fci_sink existed because an HLS core cannot expose a FIFO-backed register window (its s_axilite
-- scalars are single registers valid at ap_done). Hand-written RTL has no such limitation, and
-- fci_sink's own header already anticipated this: "If a single block design cell is preferred over
-- two, the pair can be packaged together later -- the RTL here does not change either way."
-- fci_axi4lite_regs.vhd + result_fifo.vhd provide the same register semantics, so firmware's
-- existing FciSink_* accessors keep working against the merged core at a single base address.
--
-- Datapath
-- --------
--   s_axis (16-bit signed samples, TUSER = 64-bit timestamp, TLAST on last beat of a capture)
--     -> sample_framer   pack real->complex, queue the frame's timestamp around the FFT
--     -> xfft_2048       Xilinx LogiCORE, 2048-point, block floating point, BIT-REVERSED output
--     -> bin_accumulator un-reverse the bin index, sum |Re|+|Im| over the two windows
--     -> result_fifo     32-deep {timestamp, psa_w, psa_l} records
--     -> fci_axi4lite_regs  window bounds in, frozen result window + status/watermark out
--
-- The stream input must never be backpressured for long: axis_broadcaster_0 is LOCKSTEP, so
-- stalling this core stalls psd_core and the raw-trace DMA tap with it (project log, section 4.3).
-- The FFT is configured for the full sample rate and the accumulator never stalls, so in steady
-- state tready stays high; the only backpressure possible is the FFT's own brief inter-frame gap.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.fci_core_pkg.all;

entity fci_core_rtl_top is
  generic (
    FFT_LENGTH : integer := 2048; -- must equal trigger_core's capture depth
    DATA_WIDTH : integer := 16;
    ACC_WIDTH  : integer := 32;
    FIFO_DEPTH : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- AXI4-Stream slave: trigger_core's captured trace, via axis_broadcaster_0.
    s_axis_tdata  : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    s_axis_tuser  : in  std_logic_vector(63 downto 0);
    s_axis_tlast  : in  std_logic;
    s_axis_tvalid : in  std_logic;
    s_axis_tready : out std_logic;

    -- AXI4-Lite slave. 6 address bits: the map runs to 0x2C (see fci_axi4lite_regs.vhd).
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
end entity fci_core_rtl_top;

architecture rtl of fci_core_rtl_top is

  constant BIN_WIDTH   : integer := clog2(FFT_LENGTH - 1); -- 11 for 2048
  constant LEVEL_WIDTH : integer := clog2(FIFO_DEPTH) + 1;
  constant REC_WIDTH   : integer := 64 + 2 * ACC_WIDTH;

  component xfft_2048
    port (
      aclk                        : in  std_logic;
      aresetn                     : in  std_logic;
      s_axis_config_tdata         : in  std_logic_vector(7 downto 0);
      s_axis_config_tvalid        : in  std_logic;
      s_axis_config_tready        : out std_logic;
      s_axis_data_tdata           : in  std_logic_vector(31 downto 0);
      s_axis_data_tvalid          : in  std_logic;
      s_axis_data_tready          : out std_logic;
      s_axis_data_tlast           : in  std_logic;
      m_axis_data_tdata           : out std_logic_vector(31 downto 0);
      m_axis_data_tuser           : out std_logic_vector(7 downto 0);
      m_axis_data_tvalid          : out std_logic;
      m_axis_data_tready          : in  std_logic;
      m_axis_data_tlast           : out std_logic;
      m_axis_status_tdata         : out std_logic_vector(7 downto 0);
      m_axis_status_tvalid        : out std_logic;
      m_axis_status_tready        : in  std_logic;
      event_frame_started         : out std_logic;
      event_tlast_unexpected      : out std_logic;
      event_tlast_missing         : out std_logic;
      event_status_channel_halt   : out std_logic;
      event_data_in_channel_halt  : out std_logic;
      event_data_out_channel_halt : out std_logic
    );
  end component;

  -- framer -> FFT
  signal fft_in_tdata  : std_logic_vector(2 * DATA_WIDTH - 1 downto 0);
  signal fft_in_tlast  : std_logic;
  signal fft_in_tvalid : std_logic;
  signal fft_in_tready : std_logic;

  -- FFT -> accumulator
  signal fft_out_tdata  : std_logic_vector(31 downto 0);
  signal fft_out_tvalid : std_logic;
  signal fft_out_tlast  : std_logic;

  -- FFT configuration channel
  signal cfg_tvalid : std_logic;
  signal cfg_tready : std_logic;
  signal cfg_sent   : std_logic;

  -- timestamp queue
  signal tag        : std_logic_vector(63 downto 0);
  signal tag_valid  : std_logic;

  -- accumulator -> FIFO
  signal result_valid : std_logic;
  signal psa_l        : std_logic_vector(ACC_WIDTH - 1 downto 0);
  signal psa_w        : std_logic_vector(ACC_WIDTH - 1 downto 0);

  -- window bounds from the register file
  signal psa_l_lo : std_logic_vector(BIN_WIDTH - 1 downto 0);
  signal psa_l_hi : std_logic_vector(BIN_WIDTH - 1 downto 0);
  signal psa_w_lo : std_logic_vector(BIN_WIDTH - 1 downto 0);
  signal psa_w_hi : std_logic_vector(BIN_WIDTH - 1 downto 0);

  -- FIFO
  signal fifo_din   : std_logic_vector(REC_WIDTH - 1 downto 0);
  signal fifo_dout  : std_logic_vector(REC_WIDTH - 1 downto 0);
  signal fifo_empty : std_logic;
  signal fifo_full  : std_logic;
  signal fifo_level : std_logic_vector(clog2(FIFO_DEPTH) downto 0);
  signal fifo_ovf   : std_logic;
  signal fifo_pop   : std_logic;
  signal fifo_clear : std_logic;

  signal event_count : unsigned(31 downto 0);
  signal watermark   : std_logic_vector(LEVEL_WIDTH - 1 downto 0);

begin

  -- The FFT needs one config write to select the forward transform before it will accept data.
  -- Bit 0 of s_axis_config_tdata is the direction bit (1 = forward); the rest is unused in this
  -- configuration (no run-time transform length, no per-frame scaling schedule under BFP). Sent
  -- once after reset and never again -- so a stuck config handshake is visibly a dead core rather
  -- than an intermittent one.
  cfg_tvalid <= not cfg_sent;

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        cfg_sent <= '0';
      elsif cfg_tvalid = '1' and cfg_tready = '1' then
        cfg_sent <= '1';
      end if;
    end if;
  end process;

  u_framer : entity work.sample_framer
    generic map (
      DATA_WIDTH => DATA_WIDTH,
      TAG_WIDTH  => 64,
      TAG_DEPTH  => 4,
      FFT_LENGTH => FFT_LENGTH -- the framer enforces this against the producer, see its header
    )
    port map (
      clk_i           => clk_i,
      rstn_i          => rstn_i,
      s_axis_tdata_i  => s_axis_tdata,
      s_axis_tuser_i  => s_axis_tuser,
      s_axis_tlast_i  => s_axis_tlast,
      s_axis_tvalid_i => s_axis_tvalid,
      s_axis_tready_o => s_axis_tready,
      m_axis_tdata_o  => fft_in_tdata,
      m_axis_tlast_o  => fft_in_tlast,
      m_axis_tvalid_o => fft_in_tvalid,
      m_axis_tready_i => fft_in_tready,
      tag_o           => tag,
      tag_valid_o     => tag_valid,
      result_pop_i    => result_valid
    );

  u_fft : xfft_2048
    port map (
      aclk                        => clk_i,
      aresetn                     => rstn_i,
      s_axis_config_tdata         => "00000001", -- bit0 = forward transform
      s_axis_config_tvalid        => cfg_tvalid,
      s_axis_config_tready        => cfg_tready,
      s_axis_data_tdata           => fft_in_tdata,
      s_axis_data_tvalid          => fft_in_tvalid,
      s_axis_data_tready          => fft_in_tready,
      s_axis_data_tlast           => fft_in_tlast,
      m_axis_data_tdata           => fft_out_tdata,
      m_axis_data_tuser           => open,
      m_axis_data_tvalid          => fft_out_tvalid,
      m_axis_data_tready          => '1',        -- accumulator never stalls
      m_axis_data_tlast           => fft_out_tlast,
      m_axis_status_tdata         => open,
      m_axis_status_tvalid        => open,
      m_axis_status_tready        => '1',        -- BFP exponent cancels in the ratio; drained
      event_frame_started         => open,
      event_tlast_unexpected      => open,
      event_tlast_missing         => open,
      event_status_channel_halt   => open,
      event_data_in_channel_halt  => open,
      event_data_out_channel_halt => open
    );

  -- tdata is {imag, real}, 16 bits each, exactly as the framer packed the input side.
  u_accum : entity work.bin_accumulator
    generic map (
      FFT_LENGTH => FFT_LENGTH,
      DATA_WIDTH => DATA_WIDTH,
      ACC_WIDTH  => ACC_WIDTH
    )
    port map (
      clk_i          => clk_i,
      rstn_i         => rstn_i,
      s_valid_i      => fft_out_tvalid,
      s_re_i         => fft_out_tdata(DATA_WIDTH - 1 downto 0),
      s_im_i         => fft_out_tdata(2 * DATA_WIDTH - 1 downto DATA_WIDTH),
      s_last_i       => fft_out_tlast,
      psa_l_lo_i     => psa_l_lo,
      psa_l_hi_i     => psa_l_hi,
      psa_w_lo_i     => psa_w_lo,
      psa_w_hi_i     => psa_w_hi,
      result_valid_o => result_valid,
      psa_l_o        => psa_l,
      psa_w_o        => psa_w
    );

  -- Record layout must match the register file's unpacking below.
  fifo_din <= tag & psa_w & psa_l;

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
      pop_i      => fifo_pop,
      data_o     => fifo_dout,
      empty_o    => fifo_empty,
      full_o     => fifo_full,
      level_o    => fifo_level,
      overflow_o => fifo_ovf,
      clear_i    => fifo_clear
    );

  -- Counts every frame the accumulator completed, including any the FIFO had to drop -- so
  -- comparing it against the number of results actually read tells firmware how many were lost,
  -- which the sticky overflow flag alone cannot.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        event_count <= (others => '0');
      elsif fifo_clear = '1' then
        event_count <= (others => '0');
      elsif result_valid = '1' then
        event_count <= event_count + 1;
      end if;
    end if;
  end process;

  u_regs : entity work.fci_axi4lite_regs
    generic map (
      C_ADDR_WIDTH => 6,
      BIN_WIDTH    => BIN_WIDTH,
      ACC_WIDTH    => ACC_WIDTH,
      LEVEL_WIDTH  => LEVEL_WIDTH
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
      psa_l_lo_o    => psa_l_lo,
      psa_l_hi_o    => psa_l_hi,
      psa_w_lo_o    => psa_w_lo,
      psa_w_hi_o    => psa_w_hi,
      watermark_o   => watermark,
      pop_o         => fifo_pop,
      clear_o       => fifo_clear,
      psa_l_i       => fifo_dout(ACC_WIDTH - 1 downto 0),
      psa_w_i       => fifo_dout(2 * ACC_WIDTH - 1 downto ACC_WIDTH),
      timestamp_i   => fifo_dout(REC_WIDTH - 1 downto 2 * ACC_WIDTH),
      event_count_i => std_logic_vector(event_count),
      empty_i       => fifo_empty,
      full_i        => fifo_full,
      overflow_i    => fifo_ovf,
      level_i       => fifo_level
    );

  -- Level-sensitive, matching fci_sink's semantics: asserted while at least `watermark` results
  -- are queued, so draining below the mark clears it without an explicit acknowledge. A watermark
  -- of 0 disables the interrupt entirely rather than asserting permanently.
  irq_o <= '1' when (unsigned(watermark) /= 0)
                and (unsigned(fifo_level) >= unsigned(watermark)) else '0';

end architecture rtl;
