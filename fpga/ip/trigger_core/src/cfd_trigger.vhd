-- Digital constant-fraction discriminator, as a drop-in alternative to trigger.vhd.
--
-- Why: a cross-level trigger fires when the signal crosses a fixed level, so its timing depends on
-- pulse AMPLITUDE -- a large pulse crosses the same level earlier on its rising edge than a small
-- one does. That amplitude-dependent time shift ("walk") is the dominant timing error for a
-- detector with this one's ~740 ns rise and a dynamic range spanning two decades. A CFD removes it
-- by triggering on a feature of the pulse SHAPE rather than its height.
--
-- Method: form a bipolar signal from the pulse and a delayed, attenuated copy of itself,
--
--     cfd[n] = s[n-D] - f * s[n]
--
-- and take its zero crossing. For a pulse whose shape is amplitude-independent, that crossing sits
-- at the same point on the leading edge regardless of height, because scaling s scales cfd without
-- moving its zero.
--
-- The zero crossing alone is not a trigger: baseline noise crosses zero constantly. So the raw
-- signal must first exceed an arming threshold, which opens a bounded window; the next CFD zero
-- crossing inside that window is the trigger.
--
-- SENSITIVITY CONSEQUENCE, which is not obvious and is easy to misread as a broken detector: the
-- crossing sits at a fixed n = D/(1-f) regardless of amplitude, while the threshold is crossed at
-- n = T*rise/A, which is LATER for smaller pulses. Below roughly
--
--     A_min = T * rise * (1-f) / D
--
-- the crossing therefore arrives before arming and the pulse produces no trigger at all. Choose D
-- large enough that A_min sits at or below the amplitude threshold, or the discriminator silently
-- becomes far less sensitive than the threshold implies. Measured in cfd_trigger_tb across a
-- 15x amplitude range; the default of D=24 puts A_min near 1.25x threshold. The threshold therefore keeps its old job (deciding
-- WHETHER an event is real) while the CFD decides WHEN it happened -- which is exactly the split
-- that makes the timing amplitude-independent while keeping noise rejection.
--
-- Resource shape, versus trigger.vhd:
--   * a delay line of DLY_MAX samples (SRL-mapped, as delay_line.vhd already is)
--   * one multiply for the fraction -- one DSP48 if FRAC_WIDTH is runtime-programmable, or free
--     if the fraction is fixed to a power of two (see USE_DSP)
--   * a subtractor and a sign comparison
--   * the arming comparator, which is what trigger.vhd already was
-- so it is a strict superset. See scripts/compare_trigger_area.tcl for measured numbers.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity cfd_trigger is
  generic (
    DATA_WIDTH  : integer := 16;
    DLY_MAX     : integer := 32;  -- longest CFD delay, in samples
    FRAC_WIDTH  : integer := 8;   -- fraction is frac_i / 2**FRAC_WIDTH
    ARM_WINDOW  : integer := 64;  -- samples the zero crossing may lag the arming crossing by
    USE_DSP     : boolean := true -- false = fraction forced to 1/2, no multiplier at all
  );
  port (
    clk_i     : in  std_logic;
    rstn_i    : in  std_logic;

    adc_data_i      : in  std_logic_vector(DATA_WIDTH - 1 downto 0); -- signed, baseline-restored
    arm_threshold_i : in  std_logic_vector(DATA_WIDTH - 1 downto 0); -- signed level, as before
    delay_i         : in  std_logic_vector(clog2(DLY_MAX - 1) - 1 downto 0);
    frac_i          : in  std_logic_vector(FRAC_WIDTH - 1 downto 0);
    polarity_i      : in  std_logic; -- 1 = positive-going pulses
    armed_i         : in  std_logic; -- capture engine ready, same meaning as trigger.vhd

    trigger_o : out std_logic
  );
end entity cfd_trigger;

architecture rtl of cfd_trigger is

  type dly_mem_t is array (0 to DLY_MAX - 1) of signed(DATA_WIDTH - 1 downto 0);
  signal dly_mem : dly_mem_t;

  signal sample    : signed(DATA_WIDTH - 1 downto 0);
  signal delayed   : signed(DATA_WIDTH - 1 downto 0);
  signal atten     : signed(DATA_WIDTH - 1 downto 0);
  signal cfd       : signed(DATA_WIDTH downto 0);   -- one bit of headroom for the difference
  signal cfd_q     : signed(DATA_WIDTH downto 0);

  signal arm_cnt   : unsigned(clog2(ARM_WINDOW) - 1 downto 0);
  signal armed_win : std_logic;

begin

  sample <= signed(adc_data_i);

  -- Delay line. Same structure as delay_line.vhd, which synthesises to SRLs rather than
  -- registers; DLY_MAX is small (a CFD delay is a fraction of the rise time, not a pre-trigger
  -- window) so this is much cheaper than trigger_core's 256-tap pre-trigger line.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      dly_mem(0) <= sample;
      for i in 1 to DLY_MAX - 1 loop
        dly_mem(i) <= dly_mem(i - 1);
      end loop;
    end if;
  end process;

  delayed <= dly_mem(to_integer(unsigned(delay_i)));

  -- The attenuated copy. With USE_DSP the fraction is runtime-programmable and infers one DSP48;
  -- without it, the fraction is fixed at 1/2 and costs nothing but a shift, which is the whole
  -- resource difference between a tunable and a fixed CFD.
  gen_dsp : if USE_DSP generate
    signal prod : signed(DATA_WIDTH + FRAC_WIDTH downto 0);
  begin
    prod  <= sample * signed('0' & frac_i);
    atten <= resize(shift_right(prod, FRAC_WIDTH), DATA_WIDTH);
  end generate gen_dsp;

  gen_shift : if not USE_DSP generate
    atten <= shift_right(sample, 1);
  end generate gen_shift;

  cfd <= resize(delayed, DATA_WIDTH + 1) - resize(atten, DATA_WIDTH + 1);

  process (clk_i)
    variable crossed : boolean;
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        cfd_q     <= (others => '0');
        arm_cnt   <= (others => '0');
        armed_win <= '0';
        trigger_o <= '0';
      else
        cfd_q <= cfd;

        -- Arming: the raw signal crossing the threshold opens a bounded window. Bounded rather
        -- than latched so a slow baseline excursion cannot leave the CFD permanently live.
        if (polarity_i = '1' and sample >= signed(arm_threshold_i)) or
           (polarity_i = '0' and sample <= signed(arm_threshold_i)) then
          armed_win <= '1';
          arm_cnt   <= to_unsigned(ARM_WINDOW, arm_cnt'length);
        elsif arm_cnt /= 0 then
          arm_cnt <= arm_cnt - 1;
        else
          armed_win <= '0';
        end if;

        -- Zero crossing of the bipolar signal, in the direction matching the pulse polarity.
        --
        -- Direction matters and is easy to get backwards. With cfd = s[n-D] - f*s[n] and a
        -- POSITIVE pulse: early on the leading edge the delayed copy is still at baseline while
        -- f*s[n] is already growing, so cfd starts NEGATIVE; as the delayed copy catches up it
        -- crosses UPWARD through zero. On a linear rise s[n] = k*n this is exact --
        -- cfd = k*((1-f)*n - D), zero at n = D/(1-f) -- and independent of k, which is precisely
        -- the amplitude independence being bought. So a positive pulse needs the RISING crossing.
        if polarity_i = '1' then
          crossed := (cfd_q < 0) and (cfd >= 0);
        else
          crossed := (cfd_q > 0) and (cfd <= 0);
        end if;

        if armed_i = '1' and armed_win = '1' and crossed then
          trigger_o <= '1';
          armed_win <= '0';  -- one trigger per arming window
          arm_cnt   <= (others => '0');
        else
          trigger_o <= '0';
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
