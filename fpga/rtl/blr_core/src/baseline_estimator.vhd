-- Gated exponential-moving-average baseline estimator, signed throughout.
--
-- Runs continuously, one sample per cycle, no handshake: this sits directly on the ADC sample
-- stream ahead of trigger_core, so it must never stall and never skip a sample.
--
-- Estimator
-- ---------
-- Standard leaky integrator. With the accumulator holding baseline scaled by 2^k:
--
--     acc      <- acc + sample - baseline        (only while the gate is open)
--     baseline <- acc >> k                       (arithmetic shift; the estimate is signed)
--
-- At steady state acc >> k converges to the mean of the gated input, with a time constant of
-- exactly 2^k samples. k (shift_i) is therefore the "restoration speed" knob: at 50 Msps, k=10
-- is 1024 samples = 20.5 us, k=14 is 16384 samples = 328 us. Slower than the pulse decay
-- (tau ~ 1.4 us) is the operating requirement -- otherwise the estimator tracks the pulse itself
-- and subtracts away the signal.
--
-- Signed, not offset binary
-- -------------------------
-- Samples, the accumulator and the estimate are all signed, and the restored output is centred on
-- ZERO. That is the natural representation for a bipolar pulse on a bipolar converter, and it is
-- what every consumer actually wants: psd_core integrates charge about zero, and the FFT wants a
-- zero-mean signal. Carrying an offset-binary mid-scale instead would mean every downstream block
-- subtracting the same constant back out again.
--
-- Note the arithmetic shift. `baseline <= shift_right(acc, k)` on a SIGNED accumulator replicates
-- the sign bit; the unsigned equivalent would shift zeros in and turn a small negative baseline
-- into a large positive one, which is exactly the kind of quiet sign error a gated estimator would
-- then lock onto and hold.
--
-- Gate
-- ----
-- The average must only see baseline, never pulses, or every event drags the estimate upward and
-- the restored output droops. So updates freeze whenever the sample departs from the current
-- estimate by gate_thr_i or more. gate_thr_i is the one value that has to be set from the measured
-- noise: a few sigma above it (sigma ~ 7 counts quiet, ~55 at 30 cps -- see the project log) keeps
-- the gate open on noise and shut on pulses. It is a MAGNITUDE, so it stays unsigned.
--
-- Three failure modes the gate creates on its own, all handled here rather than left to firmware:
--
--   * Cold start. acc = 0 means baseline = 0, so if the real baseline sits far from zero the very
--     first sample is thousands of counts away from the estimate, the gate shuts, and it never
--     reopens. Fixed by seeding acc directly from the first sample after reset
--     (acc <- sample << k), which makes baseline exactly equal to that sample. There is no
--     convergence period to wait out and no priming counter to size. The seed must wait for
--     sample_valid_i: sample_i arrives through a capture register, so for one cycle after reset
--     release that register still holds its reset value and does not represent the ADC at all.
--     Seeding from it captured the reset value rather than the real level, and every later check
--     failed downstream of that one wrong seed.
--   * Pulse tails reopening the gate. A threshold-only gate shuts on the pulse peak but reopens
--     part-way down the decay, while the signal is still tens of counts above baseline. Every
--     event then drags the estimate upward a little, and the restored output droops. Measured in
--     the testbench before this was added: 718 counts of drift across six 3000-count pulses.
--     Fixed by a hold-off -- once the gate closes it stays closed for holdoff_i further samples
--     after the deviation falls back inside the threshold. holdoff_i must exceed the pulse
--     duration, so ~5 decay constants: at tau ~ 1.4 us and 50 Msps that is ~350 samples.
--   * Genuine baseline drift. If the DC level walks further than gate_thr_i (temperature, or a
--     rate-dependent shift), the gate shuts and stays shut around a stale estimate. Fixed by a
--     watchdog: after the gate has been closed for 2^(k+3) consecutive cycles -- eight time
--     constants, by which point the estimate is stale by construction -- one update is forced
--     through, bleeding the estimate toward the signal until the gate reopens on its own. The
--     limit is floored at 4x the hold-off, because a watchdog shorter than the hold-off would fire
--     during every ordinary pulse and undo exactly the drift rejection the hold-off provides.
--
-- Rescaling on k changes. acc is scaled by 2^k, so changing k at runtime would otherwise leave the
-- accumulator meaning something different from what it held a cycle earlier, and the baseline would
-- jump by a factor of two. Detected here and handled by reloading acc <- baseline << k_new, which
-- preserves the estimate exactly across the change.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity baseline_estimator is
  generic (
    DATA_WIDTH : integer := 14; -- ADC resolution; samples are signed in this many bits
    MAX_SHIFT  : integer := 15
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    sample_i       : in std_logic_vector(DATA_WIDTH - 1 downto 0); -- SIGNED, every cycle
    sample_valid_i : in std_logic;                                 -- '0' until sample_i is real

    shift_i    : in std_logic_vector(3 downto 0);              -- EMA shift k, clamped 1..MAX_SHIFT
    gate_thr_i : in std_logic_vector(DATA_WIDTH - 1 downto 0); -- magnitude, unsigned
    holdoff_i  : in std_logic_vector(11 downto 0);
    hold_i     : in std_logic;

    baseline_o  : out std_logic_vector(DATA_WIDTH - 1 downto 0); -- SIGNED
    gate_open_o : out std_logic
  );
end entity baseline_estimator;

architecture rtl of baseline_estimator is

  constant ACC_WIDTH : integer := DATA_WIDTH + MAX_SHIFT;
  constant WD_WIDTH  : integer := MAX_SHIFT + 4;

  signal acc      : signed(ACC_WIDTH - 1 downto 0);
  signal baseline : signed(DATA_WIDTH - 1 downto 0);

  signal shift_q : unsigned(3 downto 0);
  signal seeded  : std_logic;

  signal wd_cnt : unsigned(WD_WIDTH - 1 downto 0);
  signal ho_cnt : unsigned(11 downto 0);

  signal gate_open : std_logic;

  -- Clamp k into [1, MAX_SHIFT]. k=0 would make the estimator follow the input exactly (baseline
  -- = sample, output identically zero), which is never useful and would mask a misprogrammed
  -- register as "working".
  function clamp_shift(v : natural) return natural is
  begin
    if v < 1 then
      return 1;
    elsif v > MAX_SHIFT then
      return MAX_SHIFT;
    else
      return v;
    end if;
  end function clamp_shift;

begin

  baseline_o  <= std_logic_vector(baseline);
  gate_open_o <= gate_open;

  process (clk_i)
    variable k        : natural range 0 to MAX_SHIFT;
    variable sample_s : signed(DATA_WIDTH - 1 downto 0);
    variable dev_v    : signed(DATA_WIDTH + 1 downto 0);
    variable absdev_v : signed(DATA_WIDTH + 1 downto 0);
    variable thr_v    : signed(DATA_WIDTH + 1 downto 0);
    variable open_now : std_logic;
    variable forced   : std_logic;
    variable wd_limit : unsigned(WD_WIDTH - 1 downto 0);
    variable ho_limit : unsigned(WD_WIDTH - 1 downto 0);
    variable ho_v     : unsigned(11 downto 0);
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        acc       <= (others => '0');
        baseline  <= (others => '0');
        shift_q   <= (others => '0');
        seeded    <= '0';
        wd_cnt    <= (others => '0');
        ho_cnt    <= (others => '0');
        gate_open <= '0';
      elsif sample_valid_i = '0' then
        null; -- capture register has not produced a real sample yet
      else
        k        := clamp_shift(to_integer(unsigned(shift_i)));
        sample_s := signed(sample_i);

        dev_v := resize(sample_s, DATA_WIDTH + 2) - resize(baseline, DATA_WIDTH + 2);
        if dev_v < 0 then
          absdev_v := -dev_v;
        else
          absdev_v := dev_v;
        end if;
        -- gate_thr_i is an unsigned magnitude, so it is zero-extended rather than sign-extended.
        thr_v := signed(resize(unsigned(gate_thr_i), DATA_WIDTH + 2));

        ho_v := ho_cnt;
        if absdev_v >= thr_v then
          ho_v     := unsigned(holdoff_i);
          open_now := '0';
        elsif ho_v /= 0 then
          ho_v     := ho_v - 1;
          open_now := '0';
        else
          open_now := '1';
        end if;
        ho_cnt    <= ho_v;
        gate_open <= open_now;

        wd_limit := shift_left(to_unsigned(8, WD_WIDTH), k);
        ho_limit := shift_left(resize(unsigned(holdoff_i), WD_WIDTH), 2);
        if ho_limit > wd_limit then
          wd_limit := ho_limit;
        end if;

        if open_now = '1' then
          wd_cnt <= (others => '0');
          forced := '0';
        elsif wd_cnt >= wd_limit then
          wd_cnt <= (others => '0');
          forced := '1';
        else
          wd_cnt <= wd_cnt + 1;
          forced := '0';
        end if;

        shift_q <= to_unsigned(k, 4);

        if seeded = '0' then
          acc      <= shift_left(resize(sample_s, ACC_WIDTH), k);
          baseline <= sample_s;
          seeded   <= '1';
        elsif to_integer(shift_q) /= k then
          acc      <= shift_left(resize(baseline, ACC_WIDTH), k);
          baseline <= baseline;
        elsif hold_i = '1' then
          acc      <= acc;
          baseline <= baseline;
        elsif open_now = '1' or forced = '1' then
          acc      <= acc + resize(sample_s, ACC_WIDTH) - resize(baseline, ACC_WIDTH);
          -- Arithmetic shift: shift_right on a signed value replicates the sign bit.
          baseline <= resize(shift_right(acc, k), DATA_WIDTH);
        else
          acc      <= acc;
          baseline <= baseline;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
