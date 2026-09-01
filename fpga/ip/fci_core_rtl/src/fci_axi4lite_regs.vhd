-- AXI4-Lite slave for the VHDL fci_core: the four bin-window bounds, plus the buffered result
-- window.
--
--   0x00 psa_l_lo      RW  narrow window, low bin  (inclusive)
--   0x04 psa_l_hi      RW  narrow window, high bin (inclusive)
--   0x08 psa_w_lo      RW  wide window, low bin    (inclusive)
--   0x0C psa_w_hi      RW  wide window, high bin   (inclusive)
--   0x10 ctrl          W   bit0 = pop one result, bit1 = clear FIFO/flags/counter (self-clearing)
--   0x14 status        RO  bit0 empty, bit1 full, bit2 overflow (sticky), bits[13:8] level
--   0x18 psa_l         RO  head of FIFO
--   0x1C psa_w         RO  head of FIFO
--   0x20 timestamp_lo  RO  head of FIFO
--   0x24 timestamp_hi  RO  head of FIFO
--   0x28 event_count   RO  frames accumulated since the last clear
--   0x2C watermark     RW  irq_o asserts once level reaches this (0 disables)
--
-- The windows are runtime-programmable on purpose. They are the one part of the FCI algorithm with
-- no derivation behind it for this detector: the defaults below were inherited from the HLS
-- testbench and are known to be mismatched to the measured pulse (tau ~ 1.4 us puts the spectral
-- corner near bin 2, so bins 1-25 and 1-90 capture almost the same energy and pin the ratio near
-- 1 -- see the project log, section 7). Retuning them must not require a rebuild.
--
-- The four result registers are the FIFO head and are frozen until ctrl.pop is written, so reading
-- psa_l, psa_w and both halves of the timestamp returns four fields from ONE event. That is why
-- the results live behind a FIFO rather than in registers the hardware overwrites in place: with
-- 32-bit AXI4-Lite reads and a 64-bit timestamp, in-place update would let a read sequence
-- straddle an event boundary and return a torn record that looks entirely plausible.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity fci_axi4lite_regs is
  generic (
    C_ADDR_WIDTH : integer := 6;
    BIN_WIDTH    : integer := 10;
    ACC_WIDTH    : integer := 32;
    LEVEL_WIDTH  : integer := 6
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

    psa_l_lo_o  : out std_logic_vector(BIN_WIDTH - 1 downto 0);
    psa_l_hi_o  : out std_logic_vector(BIN_WIDTH - 1 downto 0);
    psa_w_lo_o  : out std_logic_vector(BIN_WIDTH - 1 downto 0);
    psa_w_hi_o  : out std_logic_vector(BIN_WIDTH - 1 downto 0);
    watermark_o : out std_logic_vector(LEVEL_WIDTH - 1 downto 0);

    pop_o   : out std_logic;
    clear_o : out std_logic;

    psa_l_i       : in std_logic_vector(ACC_WIDTH - 1 downto 0);
    psa_w_i       : in std_logic_vector(ACC_WIDTH - 1 downto 0);
    timestamp_i   : in std_logic_vector(63 downto 0);
    event_count_i : in std_logic_vector(31 downto 0);
    empty_i       : in std_logic;
    full_i        : in std_logic;
    overflow_i    : in std_logic;
    level_i       : in std_logic_vector(LEVEL_WIDTH - 1 downto 0)
  );
end entity fci_axi4lite_regs;

architecture rtl of fci_axi4lite_regs is

  constant RESP_OKAY : std_logic_vector(1 downto 0) := "00";

  signal axi_awready  : std_logic;
  signal axi_wready   : std_logic;
  signal axi_bvalid   : std_logic;
  signal axi_arready  : std_logic;
  signal axi_rvalid   : std_logic;
  signal axi_araddr_q : std_logic_vector(C_ADDR_WIDTH - 1 downto 0);

  signal psa_l_lo_reg  : std_logic_vector(31 downto 0);
  signal psa_l_hi_reg  : std_logic_vector(31 downto 0);
  signal psa_w_lo_reg  : std_logic_vector(31 downto 0);
  signal psa_w_hi_reg  : std_logic_vector(31 downto 0);
  signal watermark_reg : std_logic_vector(31 downto 0);

  signal wren        : std_logic;
  signal rdata_q     : std_logic_vector(31 downto 0);
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

  psa_l_lo_o  <= psa_l_lo_reg(BIN_WIDTH - 1 downto 0);
  psa_l_hi_o  <= psa_l_hi_reg(BIN_WIDTH - 1 downto 0);
  psa_w_lo_o  <= psa_w_lo_reg(BIN_WIDTH - 1 downto 0);
  psa_w_hi_o  <= psa_w_hi_reg(BIN_WIDTH - 1 downto 0);
  watermark_o <= watermark_reg(LEVEL_WIDTH - 1 downto 0);

  status_word(0)                         <= empty_i;
  status_word(1)                         <= full_i;
  status_word(2)                         <= overflow_i;
  status_word(7 downto 3)                <= (others => '0');
  status_word(7 + LEVEL_WIDTH downto 8)  <= level_i;
  status_word(31 downto 8 + LEVEL_WIDTH) <= (others => '0');

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
        -- Inherited from fci_core_tb.cpp, NOT derived for this detector -- see the header. Bin 0
        -- (DC) is excluded by these bounds starting at 1, not by any hardware special case.
        psa_l_lo_reg  <= std_logic_vector(to_unsigned(1, 32));
        psa_l_hi_reg  <= std_logic_vector(to_unsigned(25, 32));
        psa_w_lo_reg  <= std_logic_vector(to_unsigned(1, 32));
        psa_w_hi_reg  <= std_logic_vector(to_unsigned(90, 32));
        watermark_reg <= std_logic_vector(to_unsigned(1, 32));
        pop_o         <= '0';
        clear_o       <= '0';
      else
        pop_o   <= '0';
        clear_o <= '0';
        if wren = '1' then
          case s_axi_awaddr(C_ADDR_WIDTH - 1 downto 2) is
            when "0000" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  psa_l_lo_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0001" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  psa_l_hi_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0010" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  psa_w_lo_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0011" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  psa_w_hi_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0100" =>
              pop_o   <= s_axi_wdata(0);
              clear_o <= s_axi_wdata(1);
            when "1011" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  watermark_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when others =>
              null; -- read-only addresses; writes accepted and discarded
          end case;
        end if;
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
          when "0000" => rdata_q <= psa_l_lo_reg;
          when "0001" => rdata_q <= psa_l_hi_reg;
          when "0010" => rdata_q <= psa_w_lo_reg;
          when "0011" => rdata_q <= psa_w_hi_reg;
          when "0101" => rdata_q <= status_word;
          when "0110" => rdata_q <= psa_l_i;
          when "0111" => rdata_q <= psa_w_i;
          when "1000" => rdata_q <= timestamp_i(31 downto 0);
          when "1001" => rdata_q <= timestamp_i(63 downto 32);
          when "1010" => rdata_q <= event_count_i;
          when "1011" => rdata_q <= watermark_reg;
          when others => rdata_q <= (others => '0');
        end case;
      elsif s_axi_rready = '1' and axi_rvalid = '1' then
        axi_rvalid <= '0';
      end if;
    end if;
  end process;

end architecture rtl;
