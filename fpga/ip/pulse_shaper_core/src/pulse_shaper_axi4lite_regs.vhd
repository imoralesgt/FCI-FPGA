-- 11-register AXI4-Lite slave: peaking (0x00), flat_top (0x04), decay (0x08), enable (0x0C),
-- ctrl (0x10), status (0x14), amplitude (0x18), timestamp_lo/hi (0x1C/0x20), event_count (0x24),
-- watermark (0x28).
--
-- Same single-outstanding-transaction AXI4-Lite slave pattern as psd_axi4lite_regs.vhd (this file
-- is adapted from it almost line for line): registers stored full width with byte-granular write-
-- strobe handling, ctrl's pop/clear bits self-clearing (one write = one action, no bit for
-- firmware to remember to clear itself).
--
-- peaking/flat_top/decay saturate to their SPEC ranges (not merely their bit widths) on an
-- over-range write, same reasoning as psd_axi4lite_regs.vhd's depth_o/cfd_frac_o: an out-of-range
-- value must still ARRIVE as visibly out-of-range so a firmware/host bug is caught, not silently
-- wrapped into a small in-range-looking number.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity pulse_shaper_axi4lite_regs is
  generic (
    C_ADDR_WIDTH : integer := 6;
    PEAKING_BITS  : integer := 8;
    PEAKING_MIN   : integer := 10;
    PEAKING_MAX   : integer := 128;
    FLAT_TOP_BITS : integer := 8;
    FLAT_TOP_MIN  : integer := 0;
    FLAT_TOP_MAX  : integer := 128;
    DECAY_BITS    : integer := 9;
    DECAY_MIN     : integer := 2;
    DECAY_MAX     : integer := 300;
    RECIP_BITS    : integer := 18; -- decay_recip width, signed Q2.16 (see trapezoidal_filter.vhd)
    ACC_WIDTH     : integer := 32;
    LEVEL_WIDTH   : integer := 11 -- clog2(FIFO_DEPTH)+1; FIFO_DEPTH=1024 in the block design
  );
  port (
    clk_i          : in  std_logic;
    rstn_i         : in  std_logic;

    s_axi_awaddr   : in  std_logic_vector(C_ADDR_WIDTH - 1 downto 0);
    s_axi_awvalid  : in  std_logic;
    s_axi_awready  : out std_logic;
    s_axi_wdata    : in  std_logic_vector(31 downto 0);
    s_axi_wstrb    : in  std_logic_vector(3 downto 0);
    s_axi_wvalid   : in  std_logic;
    s_axi_wready   : out std_logic;
    s_axi_bresp    : out std_logic_vector(1 downto 0);
    s_axi_bvalid   : out std_logic;
    s_axi_bready   : in  std_logic;
    s_axi_araddr   : in  std_logic_vector(C_ADDR_WIDTH - 1 downto 0);
    s_axi_arvalid  : in  std_logic;
    s_axi_arready  : out std_logic;
    s_axi_rdata    : out std_logic_vector(31 downto 0);
    s_axi_rresp    : out std_logic_vector(1 downto 0);
    s_axi_rvalid   : out std_logic;
    s_axi_rready   : in  std_logic;

    peaking_o  : out std_logic_vector(PEAKING_BITS - 1 downto 0);
    flat_top_o : out std_logic_vector(FLAT_TOP_BITS - 1 downto 0);
    -- decay_o is CLI/host bookkeeping only (raw samples, matches what $SH/$GH document) -- the
    -- filter itself is driven by decay_recip_o instead (see trapezoidal_filter.vhd's header for
    -- why a per-sample fabric divider is not the right way to turn one into the other). Firmware
    -- writes both registers together (PulseShaper_Configure()/shaper_set()'s case 2) whenever the
    -- CLI-visible `decay` parameter changes.
    decay_o    : out std_logic_vector(DECAY_BITS - 1 downto 0);
    decay_recip_o : out std_logic_vector(RECIP_BITS - 1 downto 0);
    enable_o   : out std_logic;
    watermark_o : out std_logic_vector(LEVEL_WIDTH - 1 downto 0);

    pop_o   : out std_logic;
    clear_o : out std_logic;

    -- Result FIFO head + status, presented straight through (see psd_axi4lite_regs.vhd's own
    -- comment: the FIFO's head read is combinational, so exposing it here costs no extra cycle).
    amplitude_i   : in std_logic_vector(ACC_WIDTH - 1 downto 0);
    timestamp_i   : in std_logic_vector(63 downto 0);
    event_count_i : in std_logic_vector(31 downto 0);
    empty_i       : in std_logic;
    full_i        : in std_logic;
    overflow_i    : in std_logic;
    level_i       : in std_logic_vector(LEVEL_WIDTH - 1 downto 0)
  );
end entity pulse_shaper_axi4lite_regs;

architecture rtl of pulse_shaper_axi4lite_regs is

  constant RESP_OKAY : std_logic_vector(1 downto 0) := "00";

  signal axi_awready : std_logic;
  signal axi_wready   : std_logic;
  signal axi_bvalid   : std_logic;
  signal axi_arready  : std_logic;
  signal axi_rvalid   : std_logic;
  signal axi_araddr_q : std_logic_vector(C_ADDR_WIDTH - 1 downto 0);

  signal peaking_reg  : std_logic_vector(31 downto 0);
  signal flat_top_reg : std_logic_vector(31 downto 0);
  signal decay_reg    : std_logic_vector(31 downto 0);
  signal decay_recip_reg : std_logic_vector(31 downto 0);
  signal enable_reg   : std_logic_vector(31 downto 0);
  signal watermark_reg : std_logic_vector(31 downto 0);

  signal wren    : std_logic;
  signal rdata_q : std_logic_vector(31 downto 0);

  signal status_word : std_logic_vector(31 downto 0);

  -- Saturating clamp to [lo, hi] against an unsigned 32-bit register value -- same reasoning as
  -- psd_axi4lite_regs.vhd's depth_o/cfd_frac_o: an over/under-range write must still arrive
  -- visibly out of range, not wrap into a plausible in-range value.
  function sat_clamp(v : std_logic_vector(31 downto 0); lo, hi : natural; width : integer)
    return std_logic_vector is
    variable vu : unsigned(31 downto 0) := unsigned(v);
  begin
    if vu > to_unsigned(hi, 32) then
      return std_logic_vector(to_unsigned(hi, width));
    elsif vu < to_unsigned(lo, 32) then
      return std_logic_vector(to_unsigned(lo, width));
    else
      return std_logic_vector(resize(vu, width));
    end if;
  end function sat_clamp;

begin

  s_axi_awready <= axi_awready;
  s_axi_wready  <= axi_wready;
  s_axi_bresp   <= RESP_OKAY;
  s_axi_bvalid  <= axi_bvalid;
  s_axi_arready <= axi_arready;
  s_axi_rresp   <= RESP_OKAY;
  s_axi_rvalid  <= axi_rvalid;
  s_axi_rdata   <= rdata_q;

  wren <= axi_awready and s_axi_awvalid and axi_wready and s_axi_wvalid;

  peaking_o  <= sat_clamp(peaking_reg, PEAKING_MIN, PEAKING_MAX, PEAKING_BITS);
  flat_top_o <= sat_clamp(flat_top_reg, FLAT_TOP_MIN, FLAT_TOP_MAX, FLAT_TOP_BITS);
  decay_o    <= sat_clamp(decay_reg, DECAY_MIN, DECAY_MAX, DECAY_BITS);
  -- Signed pass-through, not sat_clamp: decay_recip_reg is a firmware-computed fixed-point value
  -- (can be negative), not a plain sample count with a natural min/max to saturate against.
  decay_recip_o <= std_logic_vector(resize(signed(decay_recip_reg), RECIP_BITS));
  enable_o   <= enable_reg(0);
  watermark_o <= (others => '1')
                 when unsigned(watermark_reg) > to_unsigned(2 ** LEVEL_WIDTH - 1, 32)
                 else std_logic_vector(resize(unsigned(watermark_reg), LEVEL_WIDTH));

  build_status : process (empty_i, full_i, overflow_i, level_i)
    variable v : std_logic_vector(31 downto 0);
  begin
    v := (others => '0');
    v(0) := empty_i;
    v(1) := full_i;
    v(2) := overflow_i;
    v(8 + LEVEL_WIDTH - 1 downto 8) := level_i;
    status_word <= v;
  end process build_status;

  -- Write address/data acceptance: accept one AW+W pair at a time.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        axi_awready <= '0';
        axi_wready  <= '0';
      elsif axi_awready = '0' and s_axi_awvalid = '1' and s_axi_wvalid = '1' then
        axi_awready <= '1';
        axi_wready  <= '1';
      else
        axi_awready <= '0';
        axi_wready  <= '0';
      end if;
    end if;
  end process;

  -- Register writes, byte-granular via wstrb, plus the self-clearing ctrl strobes.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      pop_o   <= '0';
      clear_o <= '0';
      if rstn_i = '0' then
        -- Reset to a WORKING configuration, not zero: the reset defaults below (peaking=50,
        -- flat_top=20, decay=245, enable=1) are chosen from this detector's measured pulse shape
        -- (rise ~740-800 ns / ~37-40 cycles, decay tau ~4.9 us / ~245 cycles -- see the
        -- measured-pulse-shape project note) so the shaper produces a sane amplitude before
        -- firmware/host ever writes to it, the same reasoning trigger_core's CFD reset defaults
        -- follow for the same power-on-before-firmware-runs hazard.
        peaking_reg   <= std_logic_vector(to_unsigned(50, 32));
        flat_top_reg  <= std_logic_vector(to_unsigned(20, 32));
        decay_reg     <= std_logic_vector(to_unsigned(245, 32));
        -- 1/245 in Q2.16 = round(65536/245) = 267. Firmware overwrites this together with
        -- decay_reg on every PulseShaper_Configure()/shaper_set() write, but a power-on value
        -- consistent with decay_reg's own default keeps the two from disagreeing before that.
        decay_recip_reg <= std_logic_vector(to_signed(267, 32));
        enable_reg    <= std_logic_vector(to_unsigned(1, 32));
        watermark_reg <= (others => '0');
      elsif wren = '1' then
        case s_axi_awaddr(C_ADDR_WIDTH - 1 downto 2) is
          when "0000" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                peaking_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "0001" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                flat_top_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "0010" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                decay_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "0011" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                enable_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "0100" => -- ctrl: self-clearing, one write = one action
            pop_o   <= s_axi_wdata(0);
            clear_o <= s_axi_wdata(1);
          when "1010" => -- watermark
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                watermark_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "1011" => -- decay_recip (firmware-computed -1/decay, Q2.16 -- see decay_recip_o)
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                decay_recip_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when others =>
            null; -- read-only addresses; writes accepted and discarded
        end case;
      end if;
    end if;
  end process;

  -- Write response.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        axi_bvalid <= '0';
      elsif wren = '1' then
        axi_bvalid <= '1';
      elsif s_axi_bready = '1' and axi_bvalid = '1' then
        axi_bvalid <= '0';
      end if;
    end if;
  end process;

  -- Read address acceptance.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        axi_arready  <= '0';
        axi_araddr_q <= (others => '0');
      elsif axi_arready = '0' and s_axi_arvalid = '1' then
        axi_arready  <= '1';
        axi_araddr_q <= s_axi_araddr;
      else
        axi_arready <= '0';
      end if;
    end if;
  end process;

  -- Read data mux + RVALID.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        axi_rvalid <= '0';
        rdata_q    <= (others => '0');
      elsif axi_arready = '1' and s_axi_arvalid = '1' and axi_rvalid = '0' then
        axi_rvalid <= '1';
        case axi_araddr_q(C_ADDR_WIDTH - 1 downto 2) is
          when "0000" => rdata_q <= peaking_reg;
          when "0001" => rdata_q <= flat_top_reg;
          when "0010" => rdata_q <= decay_reg;
          when "0011" => rdata_q <= enable_reg;
          when "0100" => rdata_q <= (others => '0'); -- ctrl is write-only
          when "0101" => rdata_q <= status_word;
          when "0110" => rdata_q <= std_logic_vector(resize(signed(amplitude_i), 32));
          when "0111" => rdata_q <= timestamp_i(31 downto 0);
          when "1000" => rdata_q <= timestamp_i(63 downto 32);
          when "1001" => rdata_q <= event_count_i;
          when "1010" => rdata_q <= watermark_reg;
          when "1011" => rdata_q <= decay_recip_reg;
          when others => rdata_q <= (others => '0');
        end case;
      elsif s_axi_rready = '1' and axi_rvalid = '1' then
        axi_rvalid <= '0';
      end if;
    end if;
  end process;

end architecture rtl;
