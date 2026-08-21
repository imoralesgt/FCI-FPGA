-- Top level: continuous baseline restorer sitting directly on the ADC pins, ahead of trigger_core.
--
-- Position in the chain
-- ---------------------
--     adc pins -> blr_core -> trigger_core -> axis_broadcaster -> {fci_core, psd_core, ...}
--
-- blr_core is now the first block to touch the ADC bus, so the IOB-packed capture register moves
-- here from trigger_core_top along with ownership of the pins.
--
-- There is no format conversion in the chain any more. The LTC2248's MODE pin selects
-- 2's-complement output on this board, and 2's complement IS signed -- so with a signed datapath
-- the sample needs only sign extension, not the MSB flip that produced offset binary. The whole
-- offset-binary representation, and with it the double-conversion hazard that would have restored
-- the bring-up fold artifact, is designed out rather than guarded against. ADC_IS_2C survives only
-- to cover a board strapped the other way, where offset binary would need converting TO signed.
--
-- Output interface
-- ----------------
-- An AXI4-Stream master, so the block design connects blr_core to trigger_core as one interface
-- rather than as a hand-wired bundle of nets. It is AXI4-Stream for CONNECTIVITY, not for flow
-- control: m_axis_tready is an input and is deliberately ignored. This carries a continuous
-- 50 Msps converter stream that physically cannot be stalled -- there is no buffer between here
-- and the ADC pins, so a beat not taken is a sample destroyed, and honoring backpressure would
-- silently corrupt the time base rather than politely delay it. TVALID simply goes high one cycle
-- after reset and stays high forever. The same reasoning applies at trigger_core's matching slave
-- port, which ties its TREADY high.
--
-- TDATA is 16 bits with the 14-bit sample in the low bits and zeros above: AXI4-Stream requires a
-- byte-multiple TDATA width, and this matches the format trigger_core already emits downstream.
--
-- Output format
-- -------------
-- Signed, baseline restored to ZERO. That is the natural representation for a bipolar pulse and
-- it is what every consumer wants directly: psd_core integrates charge about zero, the FFT wants a
-- zero-mean input, and trigger_core compares against a signed threshold. Nothing downstream has to
-- add or subtract a mid-scale constant back out.
--
-- No saturation is needed, and none is present. The sample is ADC_WIDTH-bit signed and so is the
-- baseline, so sample - baseline spans at most +/-(2^ADC_WIDTH - 1) -- 16383 here -- which fits a
-- 16-bit signed output with a bit to spare. Overflow is structurally impossible rather than merely
-- guarded, so the clamp that an offset-binary output would have required simply does not exist.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity blr_core_top is
  generic (
    ADC_WIDTH : integer := 14;   -- matches this board's LTC2248
    -- true  = adc_data_i is 2's complement, i.e. already signed: no conversion, only sign extension
    -- false = adc_data_i is offset binary and must be converted to signed
    ADC_IS_2C : boolean := true;
    MAX_SHIFT : integer := 15
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- Raw ADC input straight from the pins, 2's complement. "Make External" in the block design.
    adc_data_i : in std_logic_vector(ADC_WIDTH - 1 downto 0);

    -- AXI4-Lite slave: shift (0x00), gate_thr (0x04), ctrl (0x08), baseline/status (0x0C, RO),
    -- holdoff (0x10).
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

    -- Baseline-restored sample stream: one beat per cycle, continuously. m_axis_tready is
    -- accepted for interface compatibility and never examined (see header).
    m_axis_tdata  : out std_logic_vector(15 downto 0);
    m_axis_tvalid : out std_logic;
    m_axis_tready : in  std_logic;

    -- Diagnostics, brought out for ILA probing rather than left buried (this project leans on the
    -- ILA heavily and an internal-only baseline would have to be inferred from captured traces).
    -- SIGNED, like everything else on this datapath -- set the ILA radix accordingly.
    baseline_o  : out std_logic_vector(ADC_WIDTH - 1 downto 0);
    gate_open_o : out std_logic
  );
end entity blr_core_top;

architecture rtl of blr_core_top is

  -- Same reasoning as trigger_core_top: register the raw bus with no combinational logic in front
  -- of this flop, so all ADC_WIDTH bits get identical pad-to-register timing. The conversion below
  -- operates on the already-registered word.
  signal adc_data_q : std_logic_vector(ADC_WIDTH - 1 downto 0);

  attribute IOB : string;
  attribute IOB of adc_data_q : signal is "TRUE";

  -- Signed sample, whatever the pins carry.
  signal sample_s : std_logic_vector(ADC_WIDTH - 1 downto 0);

  -- adc_data_q needs one cycle after reset release before it holds a real sample; until then it
  -- carries its reset value, which must not be mistaken for data (the estimator seeds from the
  -- first valid sample, so seeding from the reset value would poison the baseline permanently).
  signal sample_valid : std_logic;

  signal shift_cfg    : std_logic_vector(3 downto 0);
  signal gate_thr_cfg : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal holdoff_cfg  : std_logic_vector(11 downto 0);
  signal bypass_cfg   : std_logic;
  signal hold_cfg     : std_logic;

  signal baseline  : std_logic_vector(ADC_WIDTH - 1 downto 0);
  signal gate_open : std_logic;

  signal restored : std_logic_vector(15 downto 0);
  signal out_data : std_logic_vector(15 downto 0);

begin

  -- Present unconditionally: this stream is free-running by construction (see header).
  m_axis_tvalid <= sample_valid;
  m_axis_tdata  <= out_data;

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        adc_data_q <= (others => '0');
      else
        adc_data_q <= adc_data_i;
      end if;
    end if;
  end process;

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        sample_valid <= '0';
      else
        sample_valid <= '1';
      end if;
    end if;
  end process;

  -- 2's complement is already signed: pass it straight through.
  gen_2c : if ADC_IS_2C generate
    sample_s <= adc_data_q;
  end generate gen_2c;

  -- Offset binary differs from signed by exactly the MSB, so converting is one inversion.
  gen_ob : if not ADC_IS_2C generate
    sample_s <= (not adc_data_q(ADC_WIDTH - 1)) & adc_data_q(ADC_WIDTH - 2 downto 0);
  end generate gen_ob;

  u_estimator : entity work.baseline_estimator
    generic map (
      DATA_WIDTH => ADC_WIDTH,
      MAX_SHIFT  => MAX_SHIFT
    )
    port map (
      clk_i       => clk_i,
      rstn_i      => rstn_i,
      sample_i       => sample_s,
      sample_valid_i => sample_valid,
      shift_i     => shift_cfg,
      gate_thr_i  => gate_thr_cfg,
      holdoff_i   => holdoff_cfg,
      hold_i      => hold_cfg,
      baseline_o  => baseline,
      gate_open_o => gate_open
    );

  -- Restore: sample - baseline, signed, centred on zero. Widened to 16 bits, which cannot
  -- overflow for any ADC_WIDTH up to 15 (see header), so there is nothing to clamp.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        restored <= (others => '0');
      else
        restored <= std_logic_vector(resize(signed(sample_s), 16) - resize(signed(baseline), 16));
      end if;
    end if;
  end process;

  -- Bypass forwards the converted sample untouched, matching the restored path's one-cycle
  -- latency so switching bypass at runtime does not shift the stream in time. That equal latency
  -- is what makes an A/B comparison between restored and unrestored data meaningful.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        out_data <= (others => '0');
      elsif bypass_cfg = '1' then
        -- Sign-extended so bypassed and restored data share one representation; only the baseline
        -- subtraction differs, which is what makes the A/B comparison meaningful.
        out_data <= std_logic_vector(resize(signed(sample_s), 16));
      else
        out_data <= restored;
      end if;
    end if;
  end process;

  baseline_o  <= baseline;
  gate_open_o <= gate_open;

  u_regs : entity work.blr_axi4lite_regs
    generic map (
      C_ADDR_WIDTH => 5,
      DATA_WIDTH   => ADC_WIDTH
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
      shift_o       => shift_cfg,
      gate_thr_o    => gate_thr_cfg,
      holdoff_o     => holdoff_cfg,
      bypass_o      => bypass_cfg,
      hold_o        => hold_cfg,
      baseline_i    => baseline,
      gate_open_i   => gate_open
    );

end architecture rtl;
