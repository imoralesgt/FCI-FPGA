-- IDLE -> CAPTURE -> STREAM -> IDLE state machine. On trigger_i, writes depth_i consecutive
-- delayed_data_i samples into the circular buffer, then reads them back out over AXI4-Stream,
-- asserting tlast on the final beat. Single-buffered: armed_o is only '1' in IDLE, so a new
-- trigger cannot be accepted until the current trace has been fully streamed out (see project
-- plan for why this -- not fci_core's own throughput -- is the right design here).
--
-- STREAM read pipeline: issue_addr -> circular_buffer -> 2-entry FIFO -> AXI4-Stream
-- ---------------------------------------------------------------------------
-- circular_buffer has a 1-cycle registered read latency: the word for the address presented at
-- cycle T appears on buf_rd_data_i at T+1. An earlier version of this FSM handled that by using a
-- SINGLE `addr` register as both the buffer read address and the current-beat pointer, and holding
-- `data_valid` low for one cycle after every accepted beat while buf_rd_data_i caught up to the
-- new address. That is correct but throughput-limited to exactly one beat every two cycles: TVALID
-- toggled 1/0/1/0 forever, so a 50 Msps capture drained at 25 Msps (repo issue #10).
--
-- The fix is to stop serializing "advance address" and "present data" and pipeline them instead.
-- issue_addr runs ahead, presenting a new address every cycle it is allowed to, and returning
-- words land in a small FIFO that feeds the AXI4-Stream output. Sustained rate is then one beat
-- per cycle, with TVALID continuously high.
--
-- The FIFO needs exactly 2 entries. When tready deasserts, one read is already in flight and must
-- be absorbed (1 entry) on top of the beat already being presented (1 entry). Issue is gated on
-- the occupancy the FIFO will have NEXT cycle -- count + push - pop -- because a read issued now
-- only lands then; that is what makes overflow structurally impossible rather than merely
-- unlikely. Note this is about the CORE's own read latency, not about the downstream
-- axis_broadcaster or the DMAs: TREADY was measured steady high while TVALID toggled, so nothing
-- downstream was ever applying backpressure.
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

  signal addr        : unsigned(ADDR_WIDTH - 1 downto 0); -- CAPTURE write pointer
  signal depth_latch : unsigned(ADDR_WIDTH - 1 downto 0); -- latched (depth_i - 1), clamped

  -- STREAM read pipeline
  signal issue_addr  : unsigned(ADDR_WIDTH - 1 downto 0); -- next address to present to the buffer
  signal all_issued  : std_logic;                         -- final address has been presented
  signal flight      : std_logic;                         -- a read was issued last cycle, so
                                                          -- buf_rd_data_i is valid THIS cycle
  signal flight_last : std_logic;                         -- ...and that word is the final beat

  -- 2-entry output FIFO, sized by the read latency (see header)
  type fifo_data_t is array (0 to 1) of std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal fifo_data : fifo_data_t;
  signal fifo_last : std_logic_vector(1 downto 0);
  signal wr_ptr    : integer range 0 to 1;
  signal rd_ptr    : integer range 0 to 1;
  signal count     : integer range 0 to 2;

  signal m_valid  : std_logic;
  signal do_pop   : std_logic;
  signal do_issue : std_logic;

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

  -- Read side: the buffer's output register must only advance when we actually take a word, so
  -- rd_en tracks the issue decision rather than being held high for all of STREAM. That is what
  -- keeps buf_rd_data_i stable while a stalled FIFO drains.
  buf_rd_en_o   <= do_issue;
  buf_rd_addr_o <= std_logic_vector(issue_addr);

  m_valid <= '1' when (state = STREAM and count /= 0) else '0';
  do_pop  <= '1' when (m_valid = '1' and m_axis_tready_i = '1') else '0';

  m_axis_tdata_o(DATA_WIDTH - 1 downto 0) <= fifo_data(rd_ptr);
  m_axis_tdata_o(15 downto DATA_WIDTH)    <= (others => '0');
  m_axis_tvalid_o                         <= m_valid;
  m_axis_tlast_o                          <= '1' when (m_valid = '1' and fifo_last(rd_ptr) = '1') else '0';

  -- Issue gating. `occ` is what the FIFO will hold at the END of this cycle; a read issued now
  -- lands one cycle later, so it is only safe when that leaves a free slot (occ <= 1 of 2).
  issue_gate : process (state, all_issued, count, flight, do_pop)
    variable occ : integer range -1 to 3;
  begin
    occ := count;
    if flight = '1' then
      occ := occ + 1;
    end if;
    if do_pop = '1' then
      occ := occ - 1;
    end if;

    if state = STREAM and all_issued = '0' and occ <= 1 then
      do_issue <= '1';
    else
      do_issue <= '0';
    end if;
  end process issue_gate;

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        state       <= IDLE;
        addr        <= (others => '0');
        depth_latch <= (others => '0');
        issue_addr  <= (others => '0');
        all_issued  <= '0';
        flight      <= '0';
        flight_last <= '0';
        fifo_last   <= (others => '0');
        wr_ptr      <= 0;
        rd_ptr      <= 0;
        count       <= 0;
      else
        case state is

          when IDLE =>
            if trigger_i = '1' then
              depth_latch <= to_unsigned(clamp_depth_minus_1(to_integer(unsigned(depth_i))), ADDR_WIDTH);
              addr        <= (others => '0');
              state       <= CAPTURE;
            end if;

          when CAPTURE =>
            if addr = depth_latch then
              -- last sample written this cycle (combinational write logic above already issued
              -- it); reset the read pipeline and start streaming it back out.
              addr        <= (others => '0');
              issue_addr  <= (others => '0');
              all_issued  <= '0';
              flight      <= '0';
              flight_last <= '0';
              wr_ptr      <= 0;
              rd_ptr      <= 0;
              count       <= 0;
              state       <= STREAM;
            else
              addr <= addr + 1;
            end if;

          when STREAM =>
            -- Push: a word issued last cycle is on buf_rd_data_i now.
            if flight = '1' then
              fifo_data(wr_ptr) <= buf_rd_data_i;
              fifo_last(wr_ptr) <= flight_last;
              wr_ptr            <= 1 - wr_ptr;
            end if;

            -- Pop: the presented beat was accepted.
            if do_pop = '1' then
              rd_ptr <= 1 - rd_ptr;
            end if;

            if flight = '1' and do_pop = '0' then
              count <= count + 1;
            elsif flight = '0' and do_pop = '1' then
              count <= count - 1;
            end if;

            -- Issue: present the next address, and remember whether it is the final beat so the
            -- flag travels with the word through the pipeline rather than being recomputed later.
            flight <= do_issue;
            if do_issue = '1' then
              if issue_addr = depth_latch then
                flight_last <= '1';
                all_issued  <= '1';
              else
                flight_last <= '0';
                issue_addr  <= issue_addr + 1;
              end if;
            end if;

            -- Done once the final beat has actually been accepted downstream.
            if do_pop = '1' and fifo_last(rd_ptr) = '1' then
              state <= IDLE;
            end if;

        end case;
      end if;
    end if;
  end process;

end architecture rtl;
