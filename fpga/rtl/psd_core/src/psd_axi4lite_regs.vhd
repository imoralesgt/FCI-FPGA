-- AXI4-Lite slave for psd_core.
--
--   0x00 pre_trigger   RW  trigger position within the frame (match trigger_core's delay register)
--   0x04 pre_gate      RW  samples before the trigger included in both gates
--   0x08 short_gate    RW  short integration window length
--   0x0C long_gate     RW  long integration window length
--   0x10 baseline_ref  RW  SIGNED residual-pedestal trim, default 0 (blr_core restores to zero)
--   0x14 ctrl          W   bit0 = pop one result, bit1 = clear FIFO + counters. Self-clearing:
--                          a write acts once, so firmware never has to set-then-clear a bit.
--   0x18 status        RO  bit0 empty, bit1 full, bit2 overflow (sticky), bits[13:8] level
--   0x1C energy_short  RO  head of FIFO
--   0x20 energy_long   RO  head of FIFO
--   0x24 timestamp_lo  RO  head of FIFO
--   0x28 timestamp_hi  RO  head of FIFO
--   0x2C event_count   RO  frames integrated since the last clear
--   0x30 watermark     RW  irq_o asserts once level reaches this (0 disables)
--
-- Read-only registers present the FIFO head combinationally, so draining an event is four reads
-- plus one pop write with no handshake stalls in between.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity psd_axi4lite_regs is
  generic (
    C_ADDR_WIDTH : integer := 6;
    DATA_WIDTH   : integer := 16;
    IDX_WIDTH    : integer := 12;
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

    pre_trigger_o  : out std_logic_vector(IDX_WIDTH - 1 downto 0);
    pre_gate_o     : out std_logic_vector(IDX_WIDTH - 1 downto 0);
    short_gate_o   : out std_logic_vector(IDX_WIDTH - 1 downto 0);
    long_gate_o    : out std_logic_vector(IDX_WIDTH - 1 downto 0);
    baseline_ref_o : out std_logic_vector(DATA_WIDTH - 1 downto 0);
    watermark_o    : out std_logic_vector(LEVEL_WIDTH - 1 downto 0);

    pop_o   : out std_logic; -- one-cycle strobe
    clear_o : out std_logic; -- one-cycle strobe

    energy_short_i : in std_logic_vector(ACC_WIDTH - 1 downto 0);
    energy_long_i  : in std_logic_vector(ACC_WIDTH - 1 downto 0);
    timestamp_i    : in std_logic_vector(63 downto 0);
    event_count_i  : in std_logic_vector(31 downto 0);
    empty_i        : in std_logic;
    full_i         : in std_logic;
    overflow_i     : in std_logic;
    level_i        : in std_logic_vector(LEVEL_WIDTH - 1 downto 0)
  );
end entity psd_axi4lite_regs;

architecture rtl of psd_axi4lite_regs is

  constant RESP_OKAY : std_logic_vector(1 downto 0) := "00";


  signal axi_awready  : std_logic;
  signal axi_wready   : std_logic;
  signal axi_bvalid   : std_logic;
  signal axi_arready  : std_logic;
  signal axi_rvalid   : std_logic;
  signal axi_araddr_q : std_logic_vector(C_ADDR_WIDTH - 1 downto 0);

  signal pre_trigger_reg  : std_logic_vector(31 downto 0);
  signal pre_gate_reg     : std_logic_vector(31 downto 0);
  signal short_gate_reg   : std_logic_vector(31 downto 0);
  signal long_gate_reg    : std_logic_vector(31 downto 0);
  signal baseline_ref_reg : std_logic_vector(31 downto 0);
  signal watermark_reg    : std_logic_vector(31 downto 0);

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

  pre_trigger_o  <= pre_trigger_reg(IDX_WIDTH - 1 downto 0);
  pre_gate_o     <= pre_gate_reg(IDX_WIDTH - 1 downto 0);
  short_gate_o   <= short_gate_reg(IDX_WIDTH - 1 downto 0);
  long_gate_o    <= long_gate_reg(IDX_WIDTH - 1 downto 0);
  baseline_ref_o <= baseline_ref_reg(DATA_WIDTH - 1 downto 0);
  watermark_o    <= watermark_reg(LEVEL_WIDTH - 1 downto 0);

  status_word(0)                        <= empty_i;
  status_word(1)                        <= full_i;
  status_word(2)                        <= overflow_i;
  status_word(7 downto 3)               <= (others => '0');
  status_word(7 + LEVEL_WIDTH downto 8) <= level_i;
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
        -- Defaults describe this detector's measured pulse (rise ~21 samples, decay tau ~1.4 us =
        -- ~70 samples at 50 Msps) and trigger_core's default delay of 100: gates open 32 samples
        -- before the trigger, short covers the prompt peak, long runs out to ~5 decay constants.
        pre_trigger_reg  <= std_logic_vector(to_unsigned(100, 32));
        pre_gate_reg     <= std_logic_vector(to_unsigned(32, 32));
        short_gate_reg   <= std_logic_vector(to_unsigned(80, 32));
        long_gate_reg    <= std_logic_vector(to_unsigned(400, 32));
        -- Zero: blr_core restores the baseline to zero, so no pedestal correction is needed by
        -- default. This is a residual-offset trim, not a representation constant.
        baseline_ref_reg <= (others => '0');
        watermark_reg    <= std_logic_vector(to_unsigned(1, 32));
        pop_o            <= '0';
        clear_o          <= '0';
      else
        pop_o   <= '0';
        clear_o <= '0';
        if wren = '1' then
          case s_axi_awaddr(C_ADDR_WIDTH - 1 downto 2) is
            when "0000" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  pre_trigger_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0001" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  pre_gate_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0010" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  short_gate_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0011" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  long_gate_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0100" =>
              for b in 0 to 3 loop
                if s_axi_wstrb(b) = '1' then
                  baseline_ref_reg(b * 8 + 7 downto b * 8) <= s_axi_wdata(b * 8 + 7 downto b * 8);
                end if;
              end loop;
            when "0101" =>
              pop_o   <= s_axi_wdata(0);
              clear_o <= s_axi_wdata(1);
            when "1100" =>
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
          when "0000" => rdata_q <= pre_trigger_reg;
          when "0001" => rdata_q <= pre_gate_reg;
          when "0010" => rdata_q <= short_gate_reg;
          when "0011" => rdata_q <= long_gate_reg;
          when "0100" => rdata_q <= baseline_ref_reg;
          when "0110" => rdata_q <= status_word;
          when "0111" => rdata_q <= energy_short_i;
          when "1000" => rdata_q <= energy_long_i;
          when "1001" => rdata_q <= timestamp_i(31 downto 0);
          when "1010" => rdata_q <= timestamp_i(63 downto 32);
          when "1011" => rdata_q <= event_count_i;
          when "1100" => rdata_q <= watermark_reg;
          when others => rdata_q <= (others => '0');
        end case;
      elsif s_axi_rready = '1' and axi_rvalid = '1' then
        axi_rvalid <= '0';
      end if;
    end if;
  end process;

end architecture rtl;
