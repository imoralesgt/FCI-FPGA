-- Adapts trigger_core's captured-trace stream to the FFT IP's input stream, and carries the event
-- timestamp around the FFT.
--
-- Two jobs, both small, kept together because they are driven by the same frame boundaries:
--
-- 1. Sample format. trigger_core broadcasts 16-bit SIGNED samples, already baseline-restored to
--    zero by blr_core -- the same convention psd_core's dual_gate_integrator consumes
--    (`signed(s_data_i)`, no conversion). The FFT IP takes a 32-bit word per beat holding a
--    complex pair as {imag, real}, each 16 bits, so a real-valued input is the sample in the low
--    half and zero in the high half. NOTE this is the one place this core deliberately differs
--    from the HLS core it replaces: fci_core.cpp's axis_to_fft subtracted a mid-scale offset to
--    convert offset-binary ADC codes to signed. That conversion belongs upstream of the
--    broadcaster and blr_core already performs it; doing it again here would shift every sample by
--    half full-scale and put a large artificial step into bin 0.
--
-- 2. Timestamp passthrough. TUSER carries trigger_core's 64-bit event timestamp, held constant for
--    every beat of a capture, and the FFT IP cannot carry a sideband tag through itself. The HLS
--    core routed it around the FFT on a parallel hls::stream; here it is simply latched on the
--    first beat of a frame and held. Latching on the FIRST beat rather than the last matters: the
--    FFT's own latency means the input frame is long finished by the time the matching result
--    appears, and the NEXT capture's beats may already be arriving by then.
--
-- 3. Frame length. The FFT IP is built for a FIXED FFT_LENGTH-beat frame and treats a TLAST in the
--    wrong place as a protocol error: an early one raises event_tlast_unexpected, a late one
--    event_tlast_missing, and either HALTS its data input channel. A halted channel holds
--    s_axis_data_tready low permanently, and because axis_broadcaster_0 is lockstep that stalls
--    psd_core and axi_dma_1 too, so trigger_core never re-arms and the whole instrument is dead
--    until the bitstream is reloaded.
--
--    That is not hypothetical. trigger_core's registers reset to threshold=0, polarity=falling,
--    depth=0, and depth 0 clamps to a ONE-beat capture. On blr_core's zero-centred output a falling
--    zero crossing occurs within microseconds of configuration -- long before MicroBlaze boots and
--    writes a sane depth -- so the very first frame the FFT ever sees is 1 beat long. Forwarding
--    trigger_core's TLAST verbatim (what this block used to do) therefore bricked the pipeline at
--    power-on, every time. The HLS core this replaces was immune because it framed internally from
--    its own counter and ignored the incoming TLAST.
--
--    So this block owns the FFT's frame boundary rather than trusting the producer:
--      - TLAST to the FFT is asserted on beat FFT_LENGTH and nowhere else.
--      - A SHORT capture (upstream TLAST early) is zero-padded up to FFT_LENGTH. Padding rather
--        than dropping, because a dropped partial frame would leave the FFT mid-frame and the next
--        capture would silently complete it, mixing two events into one result -- worse than a
--        zero-padded one, which is a well-defined transform of the samples that did arrive.
--      - A LONG capture's surplus beats are accepted and discarded until upstream TLAST, so the
--        producer is never stalled and the next frame starts correctly aligned.
--    The core is then robust to any depth setting, including the reset value.
--
-- Frame-count based handoff
-- -------------------------
-- The held timestamp cannot simply be presented combinationally to whatever consumes the result:
-- with the FFT pipelined, frame N's result emerges while frame N+1 (or later) is being fed in, so
-- a single held register would hand out the WRONG event's timestamp. A small FIFO of pending
-- timestamps, pushed on each input frame's last beat and popped when a result frame completes,
-- keeps them matched however deep the FFT's pipeline runs. Depth 4 covers the IP's worst-case
-- in-flight frame count for this configuration with margin; overflow is impossible in practice
-- (the FFT backpressures the input long before four frames stack up) but is handled by dropping
-- the push rather than corrupting the ordering of the ones already queued.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.fci_core_pkg.all;

entity sample_framer is
  generic (
    DATA_WIDTH : integer := 16;   -- signed sample datapath, matching blr_core/trigger_core
    TAG_WIDTH  : integer := 64;   -- TUSER: trigger_core's event timestamp
    TAG_DEPTH  : integer := 4;    -- pending-frame timestamp queue, see header
    FFT_LENGTH : integer := 2048  -- beats per FFT frame; this block enforces it, see header
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- From axis_broadcaster (trigger_core's captured trace).
    s_axis_tdata_i  : in  std_logic_vector(DATA_WIDTH - 1 downto 0);
    s_axis_tuser_i  : in  std_logic_vector(TAG_WIDTH - 1 downto 0);
    s_axis_tlast_i  : in  std_logic;
    s_axis_tvalid_i : in  std_logic;
    s_axis_tready_o : out std_logic;

    -- To the FFT IP's data input. tdata is {imag, real}, 16 bits each.
    m_axis_tdata_o  : out std_logic_vector(2 * DATA_WIDTH - 1 downto 0);
    m_axis_tlast_o  : out std_logic;
    m_axis_tvalid_o : out std_logic;
    m_axis_tready_i : in  std_logic;

    -- Timestamp of the OLDEST frame still in flight, popped by result_pop_i when that frame's
    -- accumulation completes.
    tag_o        : out std_logic_vector(TAG_WIDTH - 1 downto 0);
    tag_valid_o  : out std_logic;
    result_pop_i : in  std_logic
  );
end entity sample_framer;

architecture rtl of sample_framer is

  constant PTR_WIDTH : integer := clog2(TAG_DEPTH);

  type tag_mem_t is array (0 to TAG_DEPTH - 1) of std_logic_vector(TAG_WIDTH - 1 downto 0);
  signal tag_mem : tag_mem_t;

  signal wr_ptr : unsigned(PTR_WIDTH - 1 downto 0);
  signal rd_ptr : unsigned(PTR_WIDTH - 1 downto 0);
  signal level  : unsigned(PTR_WIDTH downto 0);

  signal beat_accepted : std_logic;

  -- Beats already handed to the FFT for the frame in progress, 0 .. FFT_LENGTH-1.
  signal beat_count : unsigned(clog2(FFT_LENGTH) - 1 downto 0);

  -- Set once upstream has ended its capture but the FFT frame is still short: the framer then
  -- generates zero beats on its own until the frame is complete.
  signal padding : std_logic;

  -- Set once the FFT frame is complete but upstream has not yet sent its TLAST: surplus beats are
  -- accepted and discarded so the producer is never stalled.
  signal flushing : std_logic;

  signal last_beat : std_logic;

  -- This frame's timestamp, latched on its first real beat (see the push logic for why).
  signal tag_hold : std_logic_vector(TAG_WIDTH - 1 downto 0);

begin

  -- The frame boundary is OURS, not the producer's -- see header note 3. TLAST is asserted only on
  -- beat FFT_LENGTH, whatever length the capture actually was.
  last_beat <= '1' when beat_count = FFT_LENGTH - 1 else '0';

  -- While padding, the framer sources its own beats, so it must not also consume upstream ones;
  -- while flushing, it consumes upstream beats without forwarding them.
  s_axis_tready_o <= '0'            when padding = '1' else
                     '1'            when flushing = '1' else
                     m_axis_tready_i;

  m_axis_tvalid_o <= '1'            when padding = '1' else
                     '0'            when flushing = '1' else
                     s_axis_tvalid_i;

  m_axis_tlast_o  <= last_beat;

  -- Real input: sample in the low half, imaginary half zero. Padding beats are zero in both.
  m_axis_tdata_o(DATA_WIDTH - 1 downto 0) <= (others => '0') when padding = '1'
                                             else s_axis_tdata_i;
  m_axis_tdata_o(2 * DATA_WIDTH - 1 downto DATA_WIDTH) <= (others => '0');

  -- A beat actually handed to the FFT this cycle (a real one, or a generated pad beat).
  beat_accepted <= (padding and m_axis_tready_i) or
                   (s_axis_tvalid_i and m_axis_tready_i and not padding and not flushing);

  frame_ctl : process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        beat_count <= (others => '0');
        padding    <= '0';
        flushing   <= '0';
      else
        if flushing = '1' then
          -- Drop surplus beats until the producer ends its capture.
          if s_axis_tvalid_i = '1' and s_axis_tlast_i = '1' then
            flushing <= '0';
          end if;

        elsif beat_accepted = '1' then
          if last_beat = '1' then
            -- Frame complete. If this was a pad beat, or upstream ended exactly here, the next
            -- frame starts clean; otherwise drain the producer's remaining beats first.
            beat_count <= (others => '0');
            padding    <= '0';
            if padding = '0' and s_axis_tlast_i = '0' then
              flushing <= '1';
            end if;
          else
            beat_count <= beat_count + 1;
            -- Upstream ended early: finish the frame ourselves from here.
            if padding = '0' and s_axis_tvalid_i = '1' and s_axis_tlast_i = '1' then
              padding <= '1';
            end if;
          end if;
        end if;
      end if;
    end if;
  end process frame_ctl;

  tag_o       <= tag_mem(to_integer(rd_ptr));
  tag_valid_o <= '1' when level /= 0 else '0';

  process (clk_i)
    variable do_push : boolean;
    variable do_pop  : boolean;
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        wr_ptr <= (others => '0');
        rd_ptr <= (others => '0');
        level  <= (others => '0');
      else
        -- TUSER is only valid while the producer is actually streaming, so the timestamp is latched
        -- on the frame's first real beat and held. It cannot be sampled at the push below: a short
        -- capture is completed by generated pad beats, by which time the producer is idle and
        -- s_axis_tuser_i means nothing.
        if beat_accepted = '1' and beat_count = 0 and padding = '0' then
          tag_hold <= s_axis_tuser_i;
        end if;

        -- One push per completed FFT frame -- keyed on THIS block's frame boundary, not the
        -- producer's TLAST, so that padded and flushed frames each still push exactly once and the
        -- queue stays in step with the results the FFT actually emits.
        do_push := (beat_accepted = '1') and (last_beat = '1') and (level < TAG_DEPTH);
        do_pop  := (result_pop_i = '1') and (level > 0);

        if do_push then
          tag_mem(to_integer(wr_ptr)) <= tag_hold;
          if wr_ptr = TAG_DEPTH - 1 then
            wr_ptr <= (others => '0');
          else
            wr_ptr <= wr_ptr + 1;
          end if;
        end if;

        if do_pop then
          if rd_ptr = TAG_DEPTH - 1 then
            rd_ptr <= (others => '0');
          else
            rd_ptr <= rd_ptr + 1;
          end if;
        end if;

        if do_push and not do_pop then
          level <= level + 1;
        elsif do_pop and not do_push then
          level <= level - 1;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
