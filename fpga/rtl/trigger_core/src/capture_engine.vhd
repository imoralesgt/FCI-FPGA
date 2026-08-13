-- IDLE -> CAPTURE -> STREAM -> IDLE state machine. On trigger_i, writes depth_i consecutive
-- delayed_data_i samples into the circular buffer, then reads them back out over AXI4-Stream,
-- asserting tlast on the final beat. Single-buffered: armed_o is only '1' in IDLE, so a new
-- trigger cannot be accepted until the current trace has been fully streamed out (see project
-- plan for why this -- not fci_core's own throughput -- is the right design here).
--
-- Streaming read-side note: circular_buffer has a 1-cycle registered read latency, so
-- buf_rd_data_i at cycle T reflects buf_rd_addr_o from cycle T-1. buf_rd_addr_o is driven
-- combinationally from the `addr` register (no extra pipeline stage), and `data_valid` is held
-- low for exactly one cycle after any change to `addr` -- the cycle where buf_rd_data_i is still
-- catching up to the new address -- then high again once it has.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity capture_engine is
  generic (
    DATA_WIDTH : integer := 14;
    MAX_DEPTH  : integer := 4096
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    depth_i   : in  std_logic_vector(clog2(MAX_DEPTH) - 1 downto 0); -- valid range: 1..MAX_DEPTH
    trigger_i : in  std_logic;
    armed_o   : out std_logic;

    delayed_data_i : in std_logic_vector(DATA_WIDTH - 1 downto 0);

    buf_wr_en_o   : out std_logic;
    buf_wr_addr_o : out std_logic_vector(clog2(MAX_DEPTH - 1) - 1 downto 0);
    buf_wr_data_o : out std_logic_vector(DATA_WIDTH - 1 downto 0);
    buf_rd_en_o   : out std_logic;
    buf_rd_addr_o : out std_logic_vector(clog2(MAX_DEPTH - 1) - 1 downto 0);
    buf_rd_data_i : in  std_logic_vector(DATA_WIDTH - 1 downto 0);

    m_axis_tdata_o  : out std_logic_vector(15 downto 0);
    m_axis_tvalid_o : out std_logic;
    m_axis_tlast_o  : out std_logic;
    m_axis_tready_i : in  std_logic
  );
end entity capture_engine;

architecture rtl of capture_engine is

  constant ADDR_WIDTH : integer := clog2(MAX_DEPTH - 1);

  type state_t is (IDLE, CAPTURE, STREAM);
  signal state : state_t;

  signal addr        : unsigned(ADDR_WIDTH - 1 downto 0); -- write ptr in CAPTURE, read ptr in STREAM
  signal depth_latch : unsigned(ADDR_WIDTH - 1 downto 0); -- latched (depth_i - 1), clamped
  signal data_valid  : std_logic;                          -- STREAM: buf_rd_data_i matches `addr` this cycle

  function clamp_depth_minus_1(v : natural) return natural is
  begin
    if v <= 1 then
      return 0;
    elsif v > MAX_DEPTH then
      return MAX_DEPTH - 1;
    else
      return v - 1;
    end if;
  end function clamp_depth_minus_1;

begin

  armed_o <= '1' when state = IDLE else '0';

  -- Write side: purely combinational from the current state/addr, so there is no extra
  -- pipeline stage between `addr` advancing and the buffer seeing the matching address.
  buf_wr_en_o   <= '1' when state = CAPTURE else '0';
  buf_wr_addr_o <= std_logic_vector(addr);
  buf_wr_data_o <= delayed_data_i;

  -- Read side: same principle -- buf_rd_addr_o always mirrors the current `addr`.
  buf_rd_en_o   <= '1' when state = STREAM else '0';
  buf_rd_addr_o <= std_logic_vector(addr);

  m_axis_tdata_o(DATA_WIDTH - 1 downto 0) <= buf_rd_data_i;
  m_axis_tdata_o(15 downto DATA_WIDTH)    <= (others => '0');
  m_axis_tvalid_o                         <= data_valid when state = STREAM else '0';
  m_axis_tlast_o                          <= '1' when (state = STREAM and data_valid = '1' and addr = depth_latch) else '0';

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        state       <= IDLE;
        addr        <= (others => '0');
        depth_latch <= (others => '0');
        data_valid  <= '0';
      else
        case state is

          when IDLE =>
            data_valid <= '0';
            if trigger_i = '1' then
              depth_latch <= to_unsigned(clamp_depth_minus_1(to_integer(unsigned(depth_i))), ADDR_WIDTH);
              addr        <= (others => '0');
              state       <= CAPTURE;
            end if;

          when CAPTURE =>
            if addr = depth_latch then
              -- last sample written this cycle (combinational write logic above already issued
              -- it); move on to streaming it back out.
              addr       <= (others => '0');
              data_valid <= '0';
              state      <= STREAM;
            else
              addr <= addr + 1;
            end if;

          when STREAM =>
            if data_valid = '0' then
              -- `addr` changed (or STREAM just started) last cycle; buf_rd_data_i now catches up
              data_valid <= '1';
            elsif m_axis_tready_i = '1' then
              -- current beat (matching `addr`) accepted
              if addr = depth_latch then
                state      <= IDLE;
                data_valid <= '0';
              else
                addr       <= addr + 1;
                data_valid <= '0'; -- new address just issued, not valid again until next cycle
              end if;
            end if;
            -- else: data_valid='1' and tready='0' -- stall, hold `addr` (buf_rd_data_i stays
            -- stable since its address input hasn't changed)

        end case;
      end if;
    end if;
  end process;

end architecture rtl;
