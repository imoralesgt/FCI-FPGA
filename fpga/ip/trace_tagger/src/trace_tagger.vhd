-- Appends each frame's TUSER tag to the frame itself, for the raw-trace DMA.
--
-- Why: axi_dma_1 (Simple DMA mode) writes TDATA to memory and drops TUSER, so raw traces reached
-- the host with no FPGA timestamp. psd_core, fci_core and the shaper each tag their results with
-- TUSER, but a trace could only be matched to its list-mode event by comparing samples, and the
-- host could not tell a re-read of the same buffer from a new event. Carrying the tag in-band, as
-- extra beats after the frame's last sample, needs no register file, no FIFO and no change to the
-- DMA or to the other consumers: only this branch of axis_broadcaster_0 sees the extra beats.
--
-- Output frame: the input frame's beats unchanged, then TAG_BEATS beats holding the 64-bit TUSER
-- least-significant half-word first (TUSER(15:0), (31:16), (47:32), (63:48)), TLAST moved onto
-- the last tag beat. TUSER is constant across a frame (trigger_core_top), so it is latched on the
-- input's TLAST beat. Firmware must arm the DMA for depth + TAG_BEATS samples.
--
-- Cost: 64 tag flops, a 2-bit beat counter and the output mux. The input is stalled for the
-- TAG_BEATS cycles the tag takes, once per frame; the broadcaster is lockstep, so every branch
-- sees that stall, which is negligible against a frame of hundreds of samples.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity trace_tagger is
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    s_axis_tdata  : in  std_logic_vector(15 downto 0);
    s_axis_tuser  : in  std_logic_vector(63 downto 0);
    s_axis_tlast  : in  std_logic;
    s_axis_tvalid : in  std_logic;
    s_axis_tready : out std_logic;

    m_axis_tdata  : out std_logic_vector(15 downto 0);
    m_axis_tlast  : out std_logic;
    m_axis_tvalid : out std_logic;
    m_axis_tready : in  std_logic
  );
end entity trace_tagger;

architecture rtl of trace_tagger is

  constant TAG_BEATS : integer := 4;

  signal in_tag : std_logic;                  -- emitting the tag; input stalled
  signal beat   : unsigned(1 downto 0);       -- tag beat being presented
  signal tag    : std_logic_vector(63 downto 0);

begin

  -- Pass-through while not tagging: combinational, so no added latency and no buffering.
  s_axis_tready <= m_axis_tready when in_tag = '0' else '0';
  m_axis_tvalid <= s_axis_tvalid when in_tag = '0' else '1';
  m_axis_tdata  <= s_axis_tdata  when in_tag = '0' else
                   tag(15 downto 0)  when beat = 0 else
                   tag(31 downto 16) when beat = 1 else
                   tag(47 downto 32) when beat = 2 else
                   tag(63 downto 48);
  m_axis_tlast  <= '1' when in_tag = '1' and beat = TAG_BEATS - 1 else '0';

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        in_tag <= '0';
        beat   <= (others => '0');
        tag    <= (others => '0');
      elsif in_tag = '0' then
        if s_axis_tvalid = '1' and m_axis_tready = '1' and s_axis_tlast = '1' then
          tag    <= s_axis_tuser;
          beat   <= (others => '0');
          in_tag <= '1';
        end if;
      elsif m_axis_tready = '1' then
        if beat = TAG_BEATS - 1 then
          in_tag <= '0';
        end if;
        beat <= beat + 1;
      end if;
    end if;
  end process;

end architecture rtl;
