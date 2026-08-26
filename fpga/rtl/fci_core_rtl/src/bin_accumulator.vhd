-- Partial-spectrum-area accumulator: the whole of the FCI algorithm that is not the FFT.
--
-- Consumes one FFT output frame and accumulates an approximate magnitude over two independently
-- programmable bin windows. Their ratio is the Frequency Classification Index, computed on
-- MicroBlaze -- a division is not worth a divider here, and the same split is what the HLS core
-- did.
--
-- Magnitude: |Re| + |Im|
-- ----------------------
-- The alpha-max-beta-min family's simplest member. It is not the true modulus -- it overestimates
-- by up to sqrt(2) at 45 degrees -- but FCI is a RATIO of two sums of this quantity over the same
-- spectrum, so a systematic per-bin overestimate largely cancels. This is what the HLS core
-- computed (fci_core.cpp's `fixed_abs(re) + fixed_abs(im)`), and matching it is what lets the
-- existing 200-event reference dataset verify this core.
--
-- Window bounds are INCLUSIVE at both ends
-- ----------------------------------------
-- `k >= lo and k <= hi`, matching fci_core.cpp exactly. Worth stating because the sibling
-- psd_core uses a half-open [start, start+length) convention for its time-domain gates; getting
-- these two mixed up would shift a window by one bin and be nearly invisible in the output.
--
-- DC is not special-cased. Bin 0 is excluded by CONFIGURATION (the default psa_l_lo/psa_w_lo of 1),
-- not by hardware, so a caller that genuinely wants DC can have it.
--
-- Bit-reversed input ordering
-- ---------------------------
-- The FFT is configured for bit-reversed output, so beat n carries bin bit_reverse(n). The beat
-- counter is reversed before the window comparison; see fci_core_pkg for why that trade is worth
-- making.
--
-- Scale
-- -----
-- The outputs are raw sums of 17-bit magnitudes, not the HLS core's Q12.16 fixed point. The scale
-- factor is arbitrary but IDENTICAL for both windows, so the FCI ratio is unchanged -- which is
-- the only thing firmware derives from them. The FFT's block-floating-point exponent is likewise
-- shared across every bin in a frame and cancels in the same ratio, which is why it is not
-- consumed here.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.fci_core_pkg.all;

entity bin_accumulator is
  generic (
    -- Transform length in bins, not its logarithm: this is the number that has to agree with
    -- trigger_core's capture depth and with the FFT IP's own transform_length, so it is the number
    -- worth stating once and deriving everything else from.
    FFT_LENGTH : integer := 1024;
    DATA_WIDTH : integer := 16; -- FFT output width per component
    ACC_WIDTH  : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- FFT output stream. No tready: the FFT is free-running and this block is always able to
    -- accept, so there is nothing to negotiate.
    s_valid_i : in std_logic;
    s_re_i    : in std_logic_vector(DATA_WIDTH - 1 downto 0);
    s_im_i    : in std_logic_vector(DATA_WIDTH - 1 downto 0);
    s_last_i  : in std_logic;

    -- Bin-index bounds, inclusive. Width derived from FFT_LENGTH so the ports resize with the
    -- transform rather than needing to be kept in step by hand.
    psa_l_lo_i : in std_logic_vector(clog2(FFT_LENGTH - 1) - 1 downto 0);
    psa_l_hi_i : in std_logic_vector(clog2(FFT_LENGTH - 1) - 1 downto 0);
    psa_w_lo_i : in std_logic_vector(clog2(FFT_LENGTH - 1) - 1 downto 0);
    psa_w_hi_i : in std_logic_vector(clog2(FFT_LENGTH - 1) - 1 downto 0);

    result_valid_o : out std_logic;
    psa_l_o        : out std_logic_vector(ACC_WIDTH - 1 downto 0);
    psa_w_o        : out std_logic_vector(ACC_WIDTH - 1 downto 0)
  );
end entity bin_accumulator;

architecture rtl of bin_accumulator is

  -- clog2(FFT_LENGTH - 1) rather than clog2(FFT_LENGTH): for an exact power of two the latter
  -- returns one bit too many (clog2(1024) = 11), which would leave the top index bit permanently
  -- zero and silently double the address space the reversal operates over.
  constant NFFT : integer := clog2(FFT_LENGTH - 1);

  -- |x| of a DATA_WIDTH-bit signed value needs DATA_WIDTH bits unsigned (abs(-2^15) = 2^15), and
  -- their sum needs one more.
  constant MAG_WIDTH : integer := DATA_WIDTH + 1;

  signal beat_idx : unsigned(NFFT - 1 downto 0);
  signal acc_l    : unsigned(ACC_WIDTH - 1 downto 0);
  signal acc_w    : unsigned(ACC_WIDTH - 1 downto 0);

  function abs_to_unsigned(v : std_logic_vector) return unsigned is
    variable s : signed(v'length downto 0);
  begin
    -- Widen before negating so that -2^(n-1) does not overflow back onto itself.
    s := resize(signed(v), v'length + 1);
    if s < 0 then
      s := -s;
    end if;
    return unsigned(s);
  end function abs_to_unsigned;

begin

  process (clk_i)
    variable bin       : unsigned(NFFT - 1 downto 0);
    variable mag       : unsigned(MAG_WIDTH - 1 downto 0);
    variable next_l    : unsigned(ACC_WIDTH - 1 downto 0);
    variable next_w    : unsigned(ACC_WIDTH - 1 downto 0);
    variable in_l      : boolean;
    variable in_w      : boolean;
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        beat_idx       <= (others => '0');
        acc_l          <= (others => '0');
        acc_w          <= (others => '0');
        result_valid_o <= '0';
        psa_l_o        <= (others => '0');
        psa_w_o        <= (others => '0');
      else
        result_valid_o <= '0';

        if s_valid_i = '1' then
          bin := bit_reverse(beat_idx, NFFT);
          mag := resize(abs_to_unsigned(s_re_i), MAG_WIDTH)
                 + resize(abs_to_unsigned(s_im_i), MAG_WIDTH);

          in_l := (bin >= unsigned(psa_l_lo_i)) and (bin <= unsigned(psa_l_hi_i));
          in_w := (bin >= unsigned(psa_w_lo_i)) and (bin <= unsigned(psa_w_hi_i));

          if in_l then
            next_l := acc_l + resize(mag, ACC_WIDTH);
          else
            next_l := acc_l;
          end if;

          if in_w then
            next_w := acc_w + resize(mag, ACC_WIDTH);
          else
            next_w := acc_w;
          end if;

          if s_last_i = '1' then
            -- The final beat is folded into the totals above before they are published, so a
            -- window that runs to the last bin loses nothing.
            psa_l_o        <= std_logic_vector(next_l);
            psa_w_o        <= std_logic_vector(next_w);
            result_valid_o <= '1';
            acc_l          <= (others => '0');
            acc_w          <= (others => '0');
            beat_idx       <= (others => '0');
          else
            acc_l    <= next_l;
            acc_w    <= next_w;
            beat_idx <= beat_idx + 1;
          end if;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
