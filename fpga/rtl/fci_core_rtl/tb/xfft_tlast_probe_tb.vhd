-- What does the Xilinx FFT actually do when TLAST arrives in the wrong place?
--
-- This exists because a claim was made ("an early TLAST halts the input channel") and asserted as
-- documented behaviour when it was really an inference from the presence of the event_* ports plus
-- a symptom match on hardware. Rather than argue from recollection, this drives the real IP
-- simulation model with a malformed frame and records what the core does.
--
-- It answers one question with three observable outputs:
--   1. Does event_tlast_unexpected fire on a short frame?           (is the error even detected?)
--   2. Does event_data_in_channel_halt assert?                      (does it halt?)
--   3. Does s_axis_data_tready recover, and does a well-formed frame
--      that follows still produce a result?                         (is the halt PERMANENT?)
--
-- Question 3 is the one that matters. If the core resynchronises on its own, the hardware fault
-- must be explained by something else and the framer fix, while still correct hygiene, is not the
-- root cause. If it does not recover without a reset, the diagnosis stands.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity xfft_tlast_probe_tb is
end entity xfft_tlast_probe_tb;

architecture sim of xfft_tlast_probe_tb is

  constant FFT_LENGTH : integer := 2048;
  constant CLK_PERIOD : time    := 10 ns;

  signal clk      : std_logic := '0';
  signal rstn     : std_logic := '0';
  signal sim_done : std_logic := '0';

  signal cfg_tdata  : std_logic_vector(7 downto 0) := "00000001";
  signal cfg_tvalid : std_logic := '0';
  signal cfg_tready : std_logic;

  signal din_tdata  : std_logic_vector(31 downto 0) := (others => '0');
  signal din_tvalid : std_logic := '0';
  signal din_tready : std_logic;
  signal din_tlast  : std_logic := '0';

  signal dout_tvalid : std_logic;
  signal dout_tlast  : std_logic;

  signal ev_tlast_unexpected : std_logic;
  signal ev_tlast_missing    : std_logic;
  signal ev_din_halt         : std_logic;

  -- Sticky captures: the events are pulses, so latch them.
  signal saw_unexpected : boolean := false;
  signal saw_missing    : boolean := false;
  signal saw_halt       : boolean := false;
  signal result_count   : integer := 0;   -- completed result frames, monitor-owned

  component xfft_2048
    port (
      aclk                        : in  std_logic;
      aresetn                     : in  std_logic;
      s_axis_config_tdata         : in  std_logic_vector(7 downto 0);
      s_axis_config_tvalid        : in  std_logic;
      s_axis_config_tready        : out std_logic;
      s_axis_data_tdata           : in  std_logic_vector(31 downto 0);
      s_axis_data_tvalid          : in  std_logic;
      s_axis_data_tready          : out std_logic;
      s_axis_data_tlast           : in  std_logic;
      m_axis_data_tdata           : out std_logic_vector(31 downto 0);
      m_axis_data_tuser           : out std_logic_vector(7 downto 0);
      m_axis_data_tvalid          : out std_logic;
      m_axis_data_tready          : in  std_logic;
      m_axis_data_tlast           : out std_logic;
      m_axis_status_tdata         : out std_logic_vector(7 downto 0);
      m_axis_status_tvalid        : out std_logic;
      m_axis_status_tready        : in  std_logic;
      event_frame_started         : out std_logic;
      event_tlast_unexpected      : out std_logic;
      event_tlast_missing         : out std_logic;
      event_status_channel_halt   : out std_logic;
      event_data_in_channel_halt  : out std_logic;
      event_data_out_channel_halt : out std_logic
    );
  end component;

begin

  clk <= not clk after CLK_PERIOD / 2 when sim_done = '0' else '0';

  dut : xfft_2048
    port map (
      aclk                        => clk,
      aresetn                     => rstn,
      s_axis_config_tdata         => cfg_tdata,
      s_axis_config_tvalid        => cfg_tvalid,
      s_axis_config_tready        => cfg_tready,
      s_axis_data_tdata           => din_tdata,
      s_axis_data_tvalid          => din_tvalid,
      s_axis_data_tready          => din_tready,
      s_axis_data_tlast           => din_tlast,
      m_axis_data_tdata           => open,
      m_axis_data_tuser           => open,
      m_axis_data_tvalid          => dout_tvalid,
      m_axis_data_tready          => '1',
      m_axis_data_tlast           => dout_tlast,
      m_axis_status_tdata         => open,
      m_axis_status_tvalid        => open,
      m_axis_status_tready        => '1',
      event_frame_started         => open,
      event_tlast_unexpected      => ev_tlast_unexpected,
      event_tlast_missing         => ev_tlast_missing,
      event_status_channel_halt   => open,
      event_data_in_channel_halt  => ev_din_halt,
      event_data_out_channel_halt => open
    );

  -- Latch the event pulses and any result frame.
  monitor : process (clk)
  begin
    if rising_edge(clk) then
      if ev_tlast_unexpected = '1' then saw_unexpected <= true; end if;
      if ev_tlast_missing    = '1' then saw_missing    <= true; end if;
      if ev_din_halt         = '1' then saw_halt       <= true; end if;
      if dout_tvalid = '1' and dout_tlast = '1' then result_count <= result_count + 1; end if;
    end if;
  end process monitor;

  stim : process
    variable ready_cycles   : integer;
    variable results_before : integer;

    procedure beat(value : in integer; last : in std_logic) is
    begin
      din_tdata  <= std_logic_vector(to_signed(value, 16)) & x"0000";
      din_tdata(31 downto 16) <= (others => '0');
      din_tdata(15 downto 0)  <= std_logic_vector(to_signed(value, 16));
      din_tvalid <= '1';
      din_tlast  <= last;
      wait until rising_edge(clk) and din_tready = '1';
      din_tvalid <= '0';
      din_tlast  <= '0';
    end procedure beat;

  begin
    rstn <= '0';
    for i in 0 to 19 loop wait until rising_edge(clk); end loop;
    rstn <= '1';
    for i in 0 to 19 loop wait until rising_edge(clk); end loop;

    -- Configure: forward transform.
    cfg_tvalid <= '1';
    wait until rising_edge(clk) and cfg_tready = '1';
    cfg_tvalid <= '0';
    for i in 0 to 9 loop wait until rising_edge(clk); end loop;

    report "=== Driving a ONE-BEAT frame into a 2048-point FFT (TLAST on beat 1) ===";
    beat(1000, '1');
    for i in 0 to 199 loop wait until rising_edge(clk); end loop;

    report "  event_tlast_unexpected seen : " & boolean'image(saw_unexpected);
    report "  event_tlast_missing seen    : " & boolean'image(saw_missing);
    report "  event_data_in_channel_halt  : " & boolean'image(saw_halt);

    -- Does the input channel still accept data at all?
    ready_cycles := 0;
    for i in 0 to 999 loop
      wait until rising_edge(clk);
      if din_tready = '1' then ready_cycles := ready_cycles + 1; end if;
    end loop;
    report "  s_axis_data_tready high for " & integer'image(ready_cycles)
           & " of the next 1000 cycles";

    if ready_cycles = 0 then
      report "  => INPUT CHANNEL IS WEDGED: tready never returns after the bad TLAST";
    else
      report "  => input channel still accepts data after the bad TLAST";
    end if;

    -- Now a well-formed frame. Does a result come out?
    report "=== Driving a well-formed 2048-beat frame afterwards ===";
    results_before := result_count;
    wait until rising_edge(clk);
    for i in 0 to FFT_LENGTH - 1 loop
      if i = FFT_LENGTH - 1 then
        beat(i, '1');
      else
        beat(i, '0');
      end if;
    end loop;
    for i in 0 to 20 * FFT_LENGTH loop
      wait until rising_edge(clk);
      exit when result_count > results_before;
    end loop;

    report "  result frame after recovery : "
           & boolean'image(result_count > results_before);
    if result_count > results_before then
      report "VERDICT: the FFT RECOVERS on its own from a malformed TLAST.";
    else
      report "VERDICT: the FFT does NOT recover -- no result after a following good frame.";
    end if;

    sim_done <= '1';
    wait;
  end process stim;

end architecture sim;
