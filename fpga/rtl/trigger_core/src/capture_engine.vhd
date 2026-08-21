-- Double-buffered trace capture: two independent state machines sharing one dual-port buffer,
-- so a new trigger can be accepted while the previous trace is still draining.
--
-- Why this changed
-- ----------------
-- This core was deliberately SINGLE-buffered originally, and the reasoning was sound at the time:
-- fci_core's interval equals its latency (3249 cycles, no overlap), so its ceiling was ~15.4k
-- events/s, while a single-buffered trigger_core sustains 50e6/(2*depth) = 24.4k events/s at
-- depth 1024. Capturing faster than the consumer could drain would have bought nothing.
--
-- That is no longer true. With fci_core moving to a pipelined VHDL implementation (and/or a
-- higher fabric clock), trigger_core became the binding constraint instead. Overlapping capture
-- and streaming removes the factor of two: the rate becomes 50e6/depth = 48.8k events/s, because
-- the core is busy capturing for depth cycles rather than for capture + stream.
--
-- Dead time is what this actually buys. Single-buffered, every event that arrived during the
-- stream phase was lost -- half the live time at full rate. Two buffers mean an event is only
-- lost if BOTH are occupied, i.e. if a third event arrives before the first has drained.
--
-- Structure
-- ---------
-- The buffer address gains one bit, used as the buffer select, so both halves live in the same
-- dual-port RAM and no second instance is needed. The capture side owns wr_sel, the stream side
-- owns rd_sel, and a two-bit `full` flag is the only state they share:
--
--    capture FSM:  wait for trigger while buf_free  ->  write depth samples  ->  set full(wr_sel)
--    stream  FSM:  wait for full(rd_sel)            ->  stream it out        ->  clear full(rd_sel)
--
-- Each buffer carries its own latched depth, because depth_i is a live register and may change
-- between two captures that are in flight at the same time. Latching it per buffer rather than
-- once globally is what keeps a depth change from corrupting a trace already being streamed.
--
-- BRAM cost: the address space doubles. At MAX_DEPTH=4096 and a 16-bit sample that is 4 RAMB36
-- rather than 2. Setting MAX_DEPTH to 2048 -- still twice the 1024 actually used -- makes
-- double-buffering BRAM-neutral, which matters on a device already at 81% BRAM.
--
-- STREAM read pipeline (unchanged from the single-buffered version)
-- ----------------------------------------------------------------
-- circular_buffer has a 1-cycle registered read latency. An earlier version used a SINGLE `addr`
-- register as both the buffer read address and the current-beat pointer, holding `data_valid` low
-- for one cycle after every accepted beat while the RAM caught up. That is correct but limited to
-- one beat every two cycles: TVALID toggled 1/0/1/0 and a 50 Msps capture drained at 25 Msps
-- (repo issue #10). issue_addr now runs ahead and returning words land in a 2-entry FIFO, so the
-- sustained rate is one beat per cycle.
--
-- The FIFO needs exactly 2 entries: when tready deasserts, one read is already in flight and must
-- be absorbed, on top of the beat already being presented. Issue is gated on the occupancy the
-- FIFO will have NEXT cycle -- count + push - pop -- because a read issued now only lands then;
-- that is what makes overflow structurally impossible rather than merely unlikely.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity capture_engine is
  generic (
    DATA_WIDTH : integer := 16;
    MAX_DEPTH  : integer := 4096
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    depth_i   : in  std_logic_vector(clog2(MAX_DEPTH) - 1 downto 0); -- valid range: 1..MAX_DEPTH
    trigger_i : in  std_logic;
    armed_o   : out std_logic; -- '1' while at least one buffer is free

    delayed_data_i : in std_logic_vector(DATA_WIDTH - 1 downto 0);

    -- One extra address bit versus the single-buffered version: the MSB selects the buffer half.
    buf_wr_en_o   : out std_logic;
    buf_wr_addr_o : out std_logic_vector(clog2(MAX_DEPTH - 1) downto 0);
    buf_wr_data_o : out std_logic_vector(DATA_WIDTH - 1 downto 0);
    buf_rd_en_o   : out std_logic;
    buf_rd_addr_o : out std_logic_vector(clog2(MAX_DEPTH - 1) downto 0);
    buf_rd_data_i : in  std_logic_vector(DATA_WIDTH - 1 downto 0);

    m_axis_tdata_o  : out std_logic_vector(15 downto 0);
    m_axis_tvalid_o : out std_logic;
    m_axis_tlast_o  : out std_logic;
    m_axis_tready_i : in  std_logic
  );
end entity capture_engine;

architecture rtl of capture_engine is

  constant ADDR_WIDTH : integer := clog2(MAX_DEPTH - 1);

  type cap_state_t is (C_IDLE, C_CAPTURE);
  type str_state_t is (S_IDLE, S_STREAM);
  signal cap_state : cap_state_t;
  signal str_state : str_state_t;

  -- Shared between the two FSMs: which halves hold a complete, undrained trace.
  signal full : std_logic_vector(1 downto 0);

  signal wr_sel : std_logic;
  signal rd_sel : std_logic;

  signal addr : unsigned(ADDR_WIDTH - 1 downto 0); -- CAPTURE write pointer

  -- Per-buffer latched (depth_i - 1), clamped. Per buffer, not global: depth_i is a live register
  -- and a write to it must not retroactively change the length of a trace already captured.
  type depth_arr_t is array (0 to 1) of unsigned(ADDR_WIDTH - 1 downto 0);
  signal depth_latch : depth_arr_t;

  -- STREAM read pipeline
  signal issue_addr  : unsigned(ADDR_WIDTH - 1 downto 0);
  signal all_issued  : std_logic;
  signal flight      : std_logic;
  signal flight_last : std_logic;

  type fifo_data_t is array (0 to 1) of std_logic_vector(DATA_WIDTH - 1 downto 0);
  signal fifo_data : fifo_data_t;
  signal fifo_last : std_logic_vector(1 downto 0);
  signal wr_ptr    : integer range 0 to 1;
  signal rd_ptr    : integer range 0 to 1;
  signal count     : integer range 0 to 2;

  signal m_valid  : std_logic;
  signal do_pop   : std_logic;
  signal do_issue : std_logic;

  -- std_logic -> array index. Written once here rather than as unsigned'('0' & sel) at each use.
  function idx(b : std_logic) return integer is
  begin
    if b = '1' then
      return 1;
    else
      return 0;
    end if;
  end function idx;

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

  -- Armed whenever a buffer is free to receive. This is the whole point of the change: previously
  -- armed_o was '1' only in IDLE, so every event arriving during a stream was lost.
  armed_o <= '1' when (full /= "11") else '0';

  -- Write side stays combinational from the current state/addr, so there is no extra pipeline
  -- stage between `addr` advancing and the buffer seeing the matching address.
  buf_wr_en_o   <= '1' when cap_state = C_CAPTURE else '0';
  buf_wr_addr_o <= wr_sel & std_logic_vector(addr);
  buf_wr_data_o <= delayed_data_i;

  -- rd_en tracks the issue decision rather than being held high for all of STREAM, which is what
  -- keeps buf_rd_data_i stable while a stalled FIFO drains.
  buf_rd_en_o   <= do_issue;
  buf_rd_addr_o <= rd_sel & std_logic_vector(issue_addr);

  m_valid <= '1' when (str_state = S_STREAM and count /= 0) else '0';
  do_pop  <= '1' when (m_valid = '1' and m_axis_tready_i = '1') else '0';

  m_axis_tdata_o(DATA_WIDTH - 1 downto 0) <= fifo_data(rd_ptr);
  gen_pad : if DATA_WIDTH < 16 generate
    m_axis_tdata_o(15 downto DATA_WIDTH) <= (others => '0');
  end generate gen_pad;
  m_axis_tvalid_o <= m_valid;
  m_axis_tlast_o  <= '1' when (m_valid = '1' and fifo_last(rd_ptr) = '1') else '0';

  -- Issue gating. `occ` is what the FIFO will hold at the END of this cycle; a read issued now
  -- lands one cycle later, so it is only safe when that leaves a free slot (occ <= 1 of 2).
  issue_gate : process (str_state, all_issued, count, flight, do_pop)
    variable occ : integer range -1 to 3;
  begin
    occ := count;
    if flight = '1' then
      occ := occ + 1;
    end if;
    if do_pop = '1' then
      occ := occ - 1;
    end if;

    if str_state = S_STREAM and all_issued = '0' and occ <= 1 then
      do_issue <= '1';
    else
      do_issue <= '0';
    end if;
  end process issue_gate;

  -- Capture side.
  capture_fsm : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        cap_state <= C_IDLE;
        addr      <= (others => '0');
        wr_sel    <= '0';
      else
        case cap_state is

          when C_IDLE =>
            -- Accept a trigger only into a free buffer. full(wr_sel) can still be set here if the
            -- stream side has not yet drained the half we are next in line to write.
            if trigger_i = '1' and full(idx(wr_sel)) = '0' then
              depth_latch(idx(wr_sel))
                <= to_unsigned(clamp_depth_minus_1(to_integer(unsigned(depth_i))), ADDR_WIDTH);
              addr      <= (others => '0');
              cap_state <= C_CAPTURE;
            end if;

          when C_CAPTURE =>
            if addr = depth_latch(idx(wr_sel)) then
              -- Final sample written this cycle by the combinational write logic above. Hand the
              -- buffer over and move to the other half.
              addr      <= (others => '0');
              wr_sel    <= not wr_sel;
              cap_state <= C_IDLE;
            else
              addr <= addr + 1;
            end if;

        end case;
      end if;
    end if;
  end process capture_fsm;

  -- Stream side.
  stream_fsm : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        str_state   <= S_IDLE;
        rd_sel      <= '0';
        issue_addr  <= (others => '0');
        all_issued  <= '0';
        flight      <= '0';
        flight_last <= '0';
        fifo_last   <= (others => '0');
        wr_ptr      <= 0;
        rd_ptr      <= 0;
        count       <= 0;
      else
        case str_state is

          when S_IDLE =>
            if full(idx(rd_sel)) = '1' then
              issue_addr  <= (others => '0');
              all_issued  <= '0';
              flight      <= '0';
              flight_last <= '0';
              wr_ptr      <= 0;
              rd_ptr      <= 0;
              count       <= 0;
              str_state   <= S_STREAM;
            end if;

          when S_STREAM =>
            -- Push: a word issued last cycle is on buf_rd_data_i now.
            if flight = '1' then
              fifo_data(wr_ptr) <= buf_rd_data_i;
              fifo_last(wr_ptr) <= flight_last;
              wr_ptr            <= 1 - wr_ptr;
            end if;

            if do_pop = '1' then
              rd_ptr <= 1 - rd_ptr;
            end if;

            if flight = '1' and do_pop = '0' then
              count <= count + 1;
            elsif flight = '0' and do_pop = '1' then
              count <= count - 1;
            end if;

            -- Issue: present the next address, remembering whether it is the final beat so the
            -- flag travels with the word rather than being recomputed later.
            flight <= do_issue;
            if do_issue = '1' then
              if issue_addr = depth_latch(idx(rd_sel)) then
                flight_last <= '1';
                all_issued  <= '1';
              else
                flight_last <= '0';
                issue_addr  <= issue_addr + 1;
              end if;
            end if;

            -- Done once the final beat has actually been accepted downstream.
            if do_pop = '1' and fifo_last(rd_ptr) = '1' then
              rd_sel    <= not rd_sel;
              str_state <= S_IDLE;
            end if;

        end case;
      end if;
    end if;
  end process stream_fsm;

  -- The `full` flags are the handshake between the two FSMs, so they are set and cleared in one
  -- place. Set and clear can land on the same cycle for DIFFERENT buffers (capture finishing one
  -- while the stream finishes the other), which is why each bit is handled independently rather
  -- than through a shared read-modify-write.
  full_flags : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        full <= (others => '0');
      else
        -- Set and clear are separate statements, not an if/elsif chain, because they can both fire
        -- on the same cycle for DIFFERENT halves: a capture completing into one buffer while the
        -- stream finishes draining the other. They can never target the same half, since a buffer
        -- is only captured into while full(w)='0' and only streamed while full(r)='1'.
        if cap_state = C_CAPTURE and addr = depth_latch(idx(wr_sel)) then
          full(idx(wr_sel)) <= '1';
        end if;
        if str_state = S_STREAM and do_pop = '1' and fifo_last(rd_ptr) = '1' then
          full(idx(rd_sel)) <= '0';
        end if;
      end if;
    end if;
  end process full_flags;

end architecture rtl;
