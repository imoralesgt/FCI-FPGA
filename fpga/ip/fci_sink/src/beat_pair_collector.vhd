-- Collects fci_core's two-beat result into one record.
--
-- fci_core emits exactly two beats per event on m_axis_result: PSA_l with TLAST low, then PSA_w
-- with TLAST high (see fci_core.cpp's fft_to_psa, which sets beat_l.last = 0 and beat_w.last = 1).
-- This block pairs them and tags the pair with the frame's 64-bit timestamp, which fci_core
-- forwards from the input frame's TUSER.
--
-- Framing is taken from TLAST rather than from a beat counter: a counter that ever slipped by one
-- would silently swap PSA_l and PSA_w for every subsequent event, and the FCI ratio would invert
-- with nothing to indicate it. Anchoring on TLAST means the pairing re-synchronizes at the end of
-- every event, so a disturbance can corrupt at most one result rather than all of them.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity beat_pair_collector is
  generic (
    ACC_WIDTH : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- Clears the sticky framing flag and re-arms the pairing. Without this the flag latches for
    -- the lifetime of the bitstream: the FIFO and the event counter were clearable but this was
    -- not, so a single framing glitch left the status register permanently accusing.
    clear_i : in std_logic;

    s_valid_i : in std_logic;
    s_data_i  : in std_logic_vector(ACC_WIDTH - 1 downto 0);
    s_user_i  : in std_logic_vector(63 downto 0);
    s_last_i  : in std_logic;

    result_valid_o : out std_logic;
    psa_l_o        : out std_logic_vector(ACC_WIDTH - 1 downto 0);
    psa_w_o        : out std_logic_vector(ACC_WIDTH - 1 downto 0);
    timestamp_o    : out std_logic_vector(63 downto 0);

    -- A second beat arriving where a first was expected, or vice versa. Sticky, exposed as a
    -- status bit: if fci_core is ever reconfigured to emit a different number of beats, this is
    -- what says so instead of the results quietly becoming nonsense.
    framing_error_o : out std_logic
  );
end entity beat_pair_collector;

architecture rtl of beat_pair_collector is

  signal expect_first : std_logic; -- '1' when the next beat should be PSA_l
  signal psa_l_q      : std_logic_vector(ACC_WIDTH - 1 downto 0);
  signal ts_q         : std_logic_vector(63 downto 0);

begin

  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' or clear_i = '1' then
        expect_first    <= '1';
        psa_l_q         <= (others => '0');
        ts_q            <= (others => '0');
        result_valid_o  <= '0';
        psa_l_o         <= (others => '0');
        psa_w_o         <= (others => '0');
        timestamp_o     <= (others => '0');
        framing_error_o <= '0';
      else
        result_valid_o <= '0';

        if s_valid_i = '1' then
          if expect_first = '1' then
            psa_l_q <= s_data_i;
            ts_q    <= s_user_i;
            if s_last_i = '1' then
              -- A single-beat event: fci_core is not emitting the expected pair. Flag it and
              -- re-arm rather than holding a half-built record forever.
              framing_error_o <= '1';
              expect_first    <= '1';
            else
              expect_first <= '0';
            end if;
          else
            if s_last_i = '0' then
              framing_error_o <= '1';
            end if;
            psa_l_o        <= psa_l_q;
            psa_w_o        <= s_data_i;
            timestamp_o    <= ts_q;
            result_valid_o <= '1';
            expect_first   <= '1';
          end if;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
