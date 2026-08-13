-- 4-register AXI4-Lite slave: threshold (0x00), polarity (0x04), delay (0x08), depth (0x0C).
-- Standard single-outstanding-transaction AXI4-Lite slave pattern. Registers are stored full
-- width (32 bits) with byte-granular write-strobe handling; only the low bits relevant to each
-- field are driven out on the *_o ports; the rest is simply unused, never read back specially.
--
-- Reuses the top-level clk_i/rstn_i directly rather than exposing separate s_axi_aclk/aresetn
-- ports (single clock domain design) -- see project plan for why.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity axi4lite_regs is
  generic (
    C_ADDR_WIDTH : integer := 5
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

    threshold_o    : out std_logic_vector(13 downto 0);
    polarity_o     : out std_logic;
    delay_o        : out std_logic_vector(8 downto 0);
    depth_o        : out std_logic_vector(12 downto 0)
  );
end entity axi4lite_regs;

architecture rtl of axi4lite_regs is

  constant RESP_OKAY : std_logic_vector(1 downto 0) := "00";

  signal axi_awready : std_logic;
  signal axi_wready   : std_logic;
  signal axi_bvalid   : std_logic;
  signal axi_arready  : std_logic;
  signal axi_rvalid   : std_logic;
  signal axi_araddr_q : std_logic_vector(C_ADDR_WIDTH - 1 downto 0);

  signal threshold_reg : std_logic_vector(31 downto 0);
  signal polarity_reg  : std_logic_vector(31 downto 0);
  signal delay_reg     : std_logic_vector(31 downto 0);
  signal depth_reg     : std_logic_vector(31 downto 0);

  signal wren     : std_logic;
  signal rdata_q  : std_logic_vector(31 downto 0);

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

  threshold_o <= threshold_reg(13 downto 0);
  polarity_o  <= polarity_reg(0);
  delay_o     <= delay_reg(8 downto 0);
  depth_o     <= depth_reg(12 downto 0);

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

  -- Register writes, byte-granular via wstrb. Address decode on the AW address captured the
  -- same cycle wren is asserted (s_axi_awaddr is still valid then, since awready is only
  -- asserted while awvalid is high).
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        threshold_reg <= (others => '0');
        polarity_reg  <= (others => '0');
        delay_reg     <= (others => '0');
        depth_reg     <= (others => '0');
      elsif wren = '1' then
        case s_axi_awaddr(C_ADDR_WIDTH - 1 downto 2) is
          when "000" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                threshold_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "001" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                polarity_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "010" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                delay_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when "011" =>
            for b in 0 to 3 loop
              if s_axi_wstrb(b) = '1' then
                depth_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
              end if;
            end loop;
          when others =>
            null;
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
          when "000" => rdata_q <= threshold_reg;
          when "001" => rdata_q <= polarity_reg;
          when "010" => rdata_q <= delay_reg;
          when "011" => rdata_q <= depth_reg;
          when others => rdata_q <= (others => '0');
        end case;
      elsif s_axi_rready = '1' and axi_rvalid = '1' then
        axi_rvalid <= '0';
      end if;
    end if;
  end process;

end architecture rtl;
