-- Top level: pulse shaper front-end -- Jordanov-Knoll recursive trapezoidal filter
-- (trapezoidal_filter.vhd) plus the same result-FIFO/timestamp/AXI4-Lite wiring pattern
-- psd_core_top.vhd already established, adapted almost line for line (one amplitude field instead
-- of three, PEAKING/FLAT_TOP/DECAY config instead of PSD's gates).
--
-- Never backpressures -- same reasoning as psd_core_top.vhd's own header: axis_broadcaster_0 is
-- LOCKSTEP, so a stall here would stall psd_core/fci_core/the raw-trace DMA too.
--
-- Timestamp -- same in-band TUSER tagging as psd_core_top.vhd, so this core's own result can be
-- matched to the psd_core/fci_sink results computed from the same pulse (acquisition.c's
-- Acq_PopPaired() pairs all three FIFOs on this shared 64-bit timestamp).
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.pulse_shaper_core_pkg.all;

entity pulse_shaper_core_top is
  generic (
    DATA_WIDTH : integer := 16;
    ACC_WIDTH  : integer := 32;
    -- 1024, NOT the 32 psd_core_top/fci_core_rtl_top default to. Those two are overridden to 1024
    -- in the block design; this core was not, and shipped at 32 -- one thirty-second of the burst
    -- tolerance of the two FIFOs acquisition.c pairs it against. That asymmetry is not survivable
    -- here the way it would be on an independent FIFO: Acq_PopPaired() only emits an event when
    -- all three heads carry the same timestamp, so once the shallowest one fills and starts
    -- refusing events, its head is frozen in the past and pairing deadlocks outright rather than
    -- merely dropping. Defaulting to the real value removes the dependency on a BD override being
    -- remembered. registers.h's PULSE_SHAPER_STATUS_LEVEL_MASK (0x7FF, 11 bits) was already
    -- written against 1024 and matches this, where it did not match 32.
    FIFO_DEPTH : integer := 1024;
    -- 256 samples = 5.12 us at the 50 Msps sample rate these count in (they count VALID samples,
    -- gated by s_valid_i, not cycles of this core's own 150 MHz clock). The previous 128 capped
    -- shaping at 2.56 us, which is short of what this detector needs: the measured pulse decay
    -- constant is ~4.9 us, and peaking wants to sit at that order to collect the charge.
    -- Need not be a power of 2 -- see variable_delay.vhd's MEM_DEPTH -- but 256 exactly fills the
    -- array that value implies, where 250 would pay for the same 256 entries and use only 250.
    K_MAX      : integer := 256; -- peaking-time hardware ceiling, in samples
    M_MAX      : integer := 256; -- flat-top hardware ceiling, in samples
    DECAY_BITS : integer := 9;   -- CLI-visible decay register width (0..511; spec range 2..300)
    RECIP_BITS      : integer := 18; -- decay_recip (firmware-computed -1/M, Q2.16) width
    RECIP_FRAC_BITS : integer := 16
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

    -- AXI4-Lite slave; see pulse_shaper_axi4lite_regs for the map.
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

    -- Built for interface parity with psd_core/fci_core (both have one), but deliberately left
    -- UNCONNECTED in the block design: microblaze_0_xlconcat's two free inputs sit next to a
    -- documented, hardware-confirmed BSP vector-numbering bug (fpga/ublaze_sw/registers.h) that a
    -- third interrupt source risks reproducing. Firmware polls, matching how psd_core/fci_core are
    -- actually operated today regardless of their own watermark-IRQ machinery.
    irq_o : out std_logic
  );
end entity pulse_shaper_core_top;

architecture rtl of pulse_shaper_core_top is

  constant LEVEL_WIDTH : integer := clog2(FIFO_DEPTH) + 1;
  constant REC_WIDTH   : integer := 64 + ACC_WIDTH;

  signal peaking  : std_logic_vector(clog2(K_MAX) - 1 downto 0);
  signal flat_top : std_logic_vector(clog2(M_MAX) - 1 downto 0);
  signal decay    : std_logic_vector(DECAY_BITS - 1 downto 0); -- CLI/host bookkeeping only; not
                                                                -- wired to the filter (see below)
  signal decay_recip : std_logic_vector(RECIP_BITS - 1 downto 0); -- what the filter actually uses
  signal enable   : std_logic;
  signal watermark : std_logic_vector(LEVEL_WIDTH - 1 downto 0);

  signal pop_strobe   : std_logic;
  signal clear_strobe : std_logic;

  signal result_valid : std_logic;
  signal amplitude    : std_logic_vector(ACC_WIDTH - 1 downto 0);

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
  -- tlast, so it tracks frame boundaries without needing the filter's internal state.
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

  u_filter : entity work.trapezoidal_filter
    generic map (
      DATA_WIDTH      => DATA_WIDTH,
      K_MAX           => K_MAX,
      M_MAX           => M_MAX,
      RECIP_BITS      => RECIP_BITS,
      RECIP_FRAC_BITS => RECIP_FRAC_BITS,
      ACC_WIDTH       => ACC_WIDTH
    )
    port map (
      clk_i          => clk_i,
      rstn_i         => rstn_i,
      s_valid_i      => s_axis_tvalid,
      s_data_i       => s_axis_tdata(DATA_WIDTH - 1 downto 0),
      s_last_i       => s_axis_tlast,
      peaking_i      => peaking,
      flat_top_i     => flat_top,
      decay_recip_i  => decay_recip,
      enable_i       => enable,
      result_valid_o => result_valid,
      amplitude_o    => amplitude
    );

  fifo_din <= ts_latched & amplitude;

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

  -- Watermark interrupt logic: built for parity with psd_core/fci_core, but irq_o is left
  -- unconnected in the block design (see the port's own comment above).
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

  u_regs : entity work.pulse_shaper_axi4lite_regs
    generic map (
      C_ADDR_WIDTH => 6,
      PEAKING_BITS  => clog2(K_MAX),
      PEAKING_MIN   => 10,
      PEAKING_MAX   => K_MAX,
      FLAT_TOP_BITS => clog2(M_MAX),
      FLAT_TOP_MIN  => 0,
      FLAT_TOP_MAX  => M_MAX,
      DECAY_BITS    => DECAY_BITS,
      DECAY_MIN     => 2,
      DECAY_MAX     => 300,
      RECIP_BITS    => RECIP_BITS,
      ACC_WIDTH     => ACC_WIDTH,
      LEVEL_WIDTH   => LEVEL_WIDTH
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
      peaking_o      => peaking,
      flat_top_o     => flat_top,
      decay_o        => decay,
      decay_recip_o  => decay_recip,
      enable_o       => enable,
      watermark_o    => watermark,
      pop_o          => pop_strobe,
      clear_o        => clear_strobe,
      amplitude_i    => fifo_dout(ACC_WIDTH - 1 downto 0),
      timestamp_i    => fifo_dout(REC_WIDTH - 1 downto ACC_WIDTH),
      event_count_i  => std_logic_vector(event_count),
      empty_i        => fifo_empty,
      full_i         => fifo_full,
      overflow_i     => fifo_overflow,
      level_i        => fifo_level
    );

end architecture rtl;
