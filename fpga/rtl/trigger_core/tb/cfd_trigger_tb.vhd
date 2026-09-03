-- Self-checking testbench for cfd_trigger.
--
-- The property under test is the ONE that justifies replacing the cross-level trigger: the firing
-- time must not depend on pulse amplitude. Everything else the CFD does (noise rejection, polarity)
-- the old trigger did too.
--
-- Method: drive identically-shaped pulses whose amplitudes span two decades, record the sample
-- index at which the trigger fires relative to each pulse's start, and require the spread across
-- amplitudes to be at most CFD_TOL_SAMPLES. The same stimulus is also scored against what a
-- cross-level comparator would have done at a fixed threshold, so the test reports the walk being
-- removed rather than merely asserting a bound -- a CFD that silently degenerated to a level
-- trigger would still pass a bound-only check on a lucky tolerance.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.trigger_core_pkg.all;

entity cfd_trigger_tb is
end entity cfd_trigger_tb;

architecture sim of cfd_trigger_tb is

  constant DATA_WIDTH : integer := 16;
  constant DLY_MAX    : integer := 32;
  constant FRAC_WIDTH : integer := 8;
  constant ARM_WINDOW : integer := 64;
  constant CLK_PERIOD : time    := 20 ns;

  -- Pulse shape, in samples. Deliberately close to the real detector: ~37-sample 10-90% rise
  -- (740 ns at 50 Msps) and a long tail.
  constant RISE_LEN : integer := 40;
  constant PULSE_LEN : integer := 400;

  constant CFD_TOL_SAMPLES : integer := 2;

  signal clk      : std_logic := '0';
  signal rstn     : std_logic := '0';
  signal sim_done : std_logic := '0';

  signal adc_data      : std_logic_vector(DATA_WIDTH - 1 downto 0) := (others => '0');
  signal arm_threshold : std_logic_vector(DATA_WIDTH - 1 downto 0) := (others => '0');
  signal delay_sel     : std_logic_vector(clog2(DLY_MAX - 1) - 1 downto 0) :=
                           std_logic_vector(to_unsigned(12, clog2(DLY_MAX - 1)));
  signal frac_sel      : std_logic_vector(FRAC_WIDTH - 1 downto 0) :=
                           std_logic_vector(to_unsigned(64, FRAC_WIDTH)); -- 0.25
  signal polarity      : std_logic := '1';
  signal armed         : std_logic := '1';
  signal trig          : std_logic;

  signal test_count : integer := 0;
  signal fail_count : integer := 0;

begin

  clk <= not clk after CLK_PERIOD / 2 when sim_done = '0' else '0';

  dut : entity work.cfd_trigger
    generic map (DATA_WIDTH => DATA_WIDTH, DLY_MAX => DLY_MAX, FRAC_WIDTH => FRAC_WIDTH,
                 ARM_WINDOW => ARM_WINDOW, USE_DSP => true)
    port map (clk_i => clk, rstn_i => rstn, adc_data_i => adc_data,
              arm_threshold_i => arm_threshold, delay_i => delay_sel, frac_i => frac_sel,
              polarity_i => polarity, armed_i => armed, trigger_o => trig);

  stim : process
    variable fire_idx : integer;
    variable idx_min, idx_max : integer;
    variable lvl_min, lvl_max : integer;

    procedure check(msg : string; ok : boolean) is
    begin
      test_count <= test_count + 1;
      if ok then
        report "  PASS: " & msg;
      else
        report "  FAIL: " & msg severity error;
        fail_count <= fail_count + 1;
      end if;
      wait until rising_edge(clk);
    end procedure;

    -- Shape is amplitude-INDEPENDENT: sample i scaled by amp. A linear rise then exponential-ish
    -- decay, which is enough to exercise a CFD; the real pulse's exact tail does not matter here.
    function shape_at(i : integer) return real is
    begin
      if i < 0 then
        return 0.0;
      elsif i < RISE_LEN then
        return real(i) / real(RISE_LEN);
      else
        return 1.0 / (1.0 + real(i - RISE_LEN) / 120.0);
      end if;
    end function;

    -- Drives one pulse of the given amplitude, returns the sample index at which trig fired
    -- (relative to pulse start), or -1.
    procedure drive_pulse(amp : integer; fired : out integer) is
      variable v : integer;
      variable got : integer := -1;
    begin
      for i in 0 to PULSE_LEN - 1 loop
        v := integer(shape_at(i) * real(amp));
        adc_data <= std_logic_vector(to_signed(v, DATA_WIDTH));
        wait until rising_edge(clk);
        if trig = '1' and got < 0 then
          got := i;
        end if;
      end loop;
      -- return to baseline and let the arming window expire
      adc_data <= (others => '0');
      for i in 0 to ARM_WINDOW + DLY_MAX + 8 loop
        wait until rising_edge(clk);
      end loop;
      fired := got;
    end procedure;

    -- What a fixed-level comparator would have done on the same shape: first index at or above
    -- the threshold. Computed, not simulated -- this is the baseline the CFD is replacing.
    function level_cross(amp : integer; thr : integer) return integer is
    begin
      for i in 0 to PULSE_LEN - 1 loop
        if integer(shape_at(i) * real(amp)) >= thr then
          return i;
        end if;
      end loop;
      return -1;
    end function;

  begin
    arm_threshold <= std_logic_vector(to_signed(200, DATA_WIDTH));
    rstn <= '0';
    for i in 0 to 9 loop wait until rising_edge(clk); end loop;
    rstn <= '1';
    for i in 0 to 9 loop wait until rising_edge(clk); end loop;

    report "=== Amplitude independence: identical shape, amplitudes 800 .. 12000 ===";
    idx_min := integer'high; idx_max := integer'low;
    lvl_min := integer'high; lvl_max := integer'low;
    for a in 0 to 3 loop
      case a is
        when 0 => drive_pulse(800,   fire_idx);
        when 1 => drive_pulse(2400,  fire_idx);
        when 2 => drive_pulse(6000,  fire_idx);
        when others => drive_pulse(12000, fire_idx);
      end case;
      report "    amplitude index " & integer'image(a) & " fired at sample "
             & integer'image(fire_idx);
      check("pulse " & integer'image(a) & " triggered at all", fire_idx >= 0);
      if fire_idx >= 0 then
        if fire_idx < idx_min then idx_min := fire_idx; end if;
        if fire_idx > idx_max then idx_max := fire_idx; end if;
      end if;
    end loop;

    for a in 0 to 3 loop
      case a is
        when 0 => fire_idx := level_cross(800, 200);
        when 1 => fire_idx := level_cross(2400, 200);
        when 2 => fire_idx := level_cross(6000, 200);
        when others => fire_idx := level_cross(12000, 200);
      end case;
      if fire_idx < lvl_min then lvl_min := fire_idx; end if;
      if fire_idx > lvl_max then lvl_max := fire_idx; end if;
    end loop;

    report "  CFD walk over 15x amplitude range   : " & integer'image(idx_max - idx_min)
           & " samples";
    report "  cross-level walk, same stimulus     : " & integer'image(lvl_max - lvl_min)
           & " samples";
    -- Guarded: if nothing fired, idx_min/idx_max still hold their sentinel initialisers and the
    -- difference is meaningless -- an earlier run reported a flattering "1 sample" walk from
    -- integer'high minus integer'low while every pulse had actually failed to trigger.
    check("all amplitudes fired, so the walk figure is meaningful",
          idx_min <= idx_max and idx_min /= integer'high);
    check("CFD walk <= " & integer'image(CFD_TOL_SAMPLES) & " samples",
          idx_min /= integer'high and (idx_max - idx_min) <= CFD_TOL_SAMPLES);
    check("CFD walk is smaller than a level trigger's on the same pulses",
          idx_min /= integer'high and (idx_max - idx_min) < (lvl_max - lvl_min));

    -- Efficiency near threshold. The CFD zero crossing sits at a FIXED sample index, while the
    -- arming threshold is crossed at an index that depends on amplitude -- so for a small enough
    -- pulse the crossing arrives BEFORE arming and is missed entirely. That is a real sensitivity
    -- hole, not a rounding effect, and its size is set by the CFD delay: minimum triggerable
    -- amplitude ~= T * rise * (1-f) / D. At the default D=12 that is 2.5x threshold; at D=28 it is
    -- 1.07x. This test measures where the edge actually falls so the number is not merely asserted.
    report "=== Sensitivity near threshold (delay = 12) ===";
    for a in 0 to 5 loop
      case a is
        when 0 => drive_pulse(1600, fire_idx);
        when 1 => drive_pulse(1000, fire_idx);
        when 2 => drive_pulse(700,  fire_idx);
        when 3 => drive_pulse(550,  fire_idx);
        when 4 => drive_pulse(450,  fire_idx);
        when others => drive_pulse(350, fire_idx);
      end case;
      report "    amplitude case " & integer'image(a) & " -> fired at "
             & integer'image(fire_idx);
    end loop;

    report "=== A longer CFD delay recovers the small pulses ===";
    delay_sel <= std_logic_vector(to_unsigned(28, delay_sel'length));
    for i in 0 to 4 loop wait until rising_edge(clk); end loop;
    drive_pulse(450, fire_idx);
    check("amplitude 450 (2.25x threshold) triggers with delay=28", fire_idx >= 0);
    drive_pulse(350, fire_idx);
    check("amplitude 350 (1.75x threshold) triggers with delay=28", fire_idx >= 0);
    delay_sel <= std_logic_vector(to_unsigned(12, delay_sel'length));
    for i in 0 to 4 loop wait until rising_edge(clk); end loop;

    report "=== Noise below the arming threshold must not trigger ===";
    for i in 0 to 400 loop
      -- +/-150, comfortably under the 200 arming level, and crossing zero constantly so the CFD
      -- itself sees plenty of zero crossings. Only the arming gate stops these becoming triggers.
      if (i mod 2) = 0 then
        adc_data <= std_logic_vector(to_signed(150, DATA_WIDTH));
      else
        adc_data <= std_logic_vector(to_signed(-150, DATA_WIDTH));
      end if;
      wait until rising_edge(clk);
      if trig = '1' then
        fail_count <= fail_count + 1;
        report "  FAIL: noise below threshold produced a trigger" severity error;
        exit;
      end if;
    end loop;
    adc_data <= (others => '0');
    check("sub-threshold noise produced no trigger", true);

    report "=== Not armed: no trigger even on a valid pulse ===";
    armed <= '0';
    drive_pulse(6000, fire_idx);
    check("armed_i = 0 suppresses the trigger", fire_idx < 0);
    armed <= '1';

    ---------------------------------------------------------------------------
    wait until rising_edge(clk);
    report "=== " & integer'image(test_count) & " tests run, " & integer'image(fail_count)
           & " failed ===";
    if fail_count = 0 then
      report "TEST PASSED";
    else
      report "TEST FAILED" severity error;
    end if;
    sim_done <= '1';
    wait;
  end process stim;

end architecture sim;
