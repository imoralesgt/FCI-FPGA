-- 5-register AXI4-Lite slave for blr_core: shift (0x00), gate_thr (0x04), ctrl (0x08),
-- baseline/status (0x0C, read-only), holdoff (0x10).
--
-- Same single-outstanding-transaction pattern as trigger_core's axi4lite_regs, and likewise reuses
-- the top-level clk_i/rstn_i rather than exposing separate s_axi_aclk/aresetn (single clock domain).
--
-- 0x0C is read-only and reads the LIVE estimator output rather than a stored register. That is the
-- point of it: firmware can watch the baseline converge during bring-up, and can read back what the
-- core actually settled on instead of inferring it from a captured trace.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity blr_axi4lite_regs is
  generic (
    C_ADDR_WIDTH : integer := 5;
    DATA_WIDTH   : integer := 14
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    s_axi_awaddr  : in  std_logic_vector(C_ADDR_WIDTH - 1 downto 0);
    s_axi_awvalid : in  std_logic;
    s_axi_awready : out std_logic;
    s_axi_wdata   : in  std_logic_vector(31 downto 0);
    s_axi_wstrb   : in  std_logic_vector(3 downto 0);
    s_axi_wvalid  : in  std_logic;
    s_axi_wready  : out std_logic;
    s_axi_bresp   : out std_logic_vector(1 downto 0);
    s_axi_bvalid  : out std_logic;
    s_axi_bready  : in  std_logic;
    s_axi_araddr  : in  std_logic_vector(C_ADDR_WIDTH - 1 downto 0);
    s_axi_arvalid : in  std_logic;
    s_axi_arready : out std_logic;
    s_axi_rdata   : out std_logic_vector(31 downto 0);
    s_axi_rresp   : out std_logic_vector(1 downto 0);
    s_axi_rvalid  : out std_logic;
    s_axi_rready  : in  std_logic;

    shift_o    : out std_logic_vector(3 downto 0);
    gate_thr_o : out std_logic_vector(DATA_WIDTH - 1 downto 0);
    holdoff_o  : out std_logic_vector(11 downto 0);
    bypass_o   : out std_logic;
    hold_o     : out std_logic;

    baseline_i  : in std_logic_vector(DATA_WIDTH - 1 downto 0);
    gate_open_i : in std_logic
  );
end entity blr_axi4lite_regs;

architecture rtl of blr_axi4lite_regs is

  constant RESP_OKAY : std_logic_vector(1 downto 0) := "00";

  signal axi_awready  : std_logic;
  signal axi_wready   : std_logic;
  signal axi_bvalid   : std_logic;
  signal axi_arready  : std_logic;
  signal axi_rvalid   : std_logic;
  signal axi_araddr_q : std_logic_vector(C_ADDR_WIDTH - 1 downto 0);

  signal shift_reg    : std_logic_vector(31 downto 0);
  signal gate_thr_reg : std_logic_vector(31 downto 0);
  signal ctrl_reg     : std_logic_vector(31 downto 0);
  signal holdoff_reg  : std_logic_vector(31 downto 0);

  signal wren    : std_logic;
  signal rdata_q : std_logic_vector(31 downto 0);

  signal status_word : std_logic_vector(31 downto 0);

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

  shift_o    <= shift_reg(3 downto 0);
  gate_thr_o <= gate_thr_reg(DATA_WIDTH - 1 downto 0);
  holdoff_o  <= holdoff_reg(11 downto 0);
  bypass_o   <= ctrl_reg(0);
  hold_o     <= ctrl_reg(1);

  -- 0x0C: live baseline in the low bits, gate state alongside it so one read answers both
  -- "what is the estimate" and "is it currently tracking".
  status_word(DATA_WIDTH - 1 downto 0)  <= baseline_i;
  status_word(30 downto DATA_WIDTH)     <= (others => '0');
  status_word(31)                       <= gate_open_i;

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

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        -- Reset defaults are deliberately a usable configuration, not zero: shift 0 would be
        -- clamped anyway, and a zero gate threshold would hold the gate shut until firmware
        -- intervened. k=12 (4096 samples = 82 us at 50 Msps) is slow against the ~1.4 us pulse
        -- decay, and 256 counts is well above the measured baseline sigma.
        shift_reg    <= std_logic_vector(to_unsigned(12, 32));
        gate_thr_reg <= std_logic_vector(to_unsigned(256, 32));
        ctrl_reg     <= (others => '0');
        -- 384 samples = 7.7 us at 50 Msps, comfortably past 5 decay constants of the measured
        -- ~1.4 us pulse, so the gate cannot reopen on a pulse tail at the default settings.
        holdoff_reg  <= std_logic_vector(to_unsigned(384, 32));
      elsif wren = '1' then
        case s_axi_awaddr(C_ADDR_WIDTH - 1 downto 2) is
          when "000" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                shift_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "001" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                gate_thr_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "010" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                ctrl_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "100" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                holdoff_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when others =>
            null; -- 0x0C is read-only; writes to it are accepted and discarded
        end case;
      end if;
    end if;
  end process;

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

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        axi_rvalid <= '0';
        rdata_q    <= (others => '0');
      elsif axi_arready = '1' and s_axi_arvalid = '1' and axi_rvalid = '0' then
        axi_rvalid <= '1';
        case axi_araddr_q(C_ADDR_WIDTH - 1 downto 2) is
          when "000"  => rdata_q <= shift_reg;
          when "001"  => rdata_q <= gate_thr_reg;
          when "010"  => rdata_q <= ctrl_reg;
          when "011"  => rdata_q <= status_word;
          when "100"  => rdata_q <= holdoff_reg;
          when others => rdata_q <= (others => '0');
        end case;
      elsif s_axi_rready = '1' and axi_rvalid = '1' then
        axi_rvalid <= '0';
      end if;
    end if;
  end process;

end architecture rtl;
