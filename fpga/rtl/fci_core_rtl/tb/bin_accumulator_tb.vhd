-- Self-checking testbench for bin_accumulator -- the whole of the FCI algorithm except the FFT,
-- exercised with the plain xvhdl/xelab/xsim flow used by the other hand-written cores.
--
-- Independence of the expectation: the DUT is driven in BEAT order and checked against sums the
-- testbench computes in BIN order, applying its own bit reversal. If the DUT's reversal were
-- wrong, a testbench that generated stimulus and expectation through the same reversal would
-- cancel the error out and pass. Driving one way and predicting the other is what makes the index
-- mapping actually tested.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity bin_accumulator_tb is
end entity bin_accumulator_tb;

architecture sim of bin_accumulator_tb is

  constant FFT_LENGTH : integer := 1024;
  constant NBINS      : integer := FFT_LENGTH;
  constant NFFT       : integer := 10; -- log2(FFT_LENGTH), stated independently of the DUT
  constant DATA_WIDTH : integer := 16;
  constant ACC_WIDTH  : integer := 32;
  constant CLK_PERIOD : time := 20 ns;

  signal clk_i  : std_logic := '0';
  signal rstn_i : std_logic := '0';

  signal s_valid_i : std_logic := '0';
  signal s_re_i    : std_logic_vector(DATA_WIDTH - 1 downto 0) := (others => '0');
  signal s_im_i    : std_logic_vector(DATA_WIDTH - 1 downto 0) := (others => '0');
  signal s_last_i  : std_logic := '0';

  signal psa_l_lo_i : std_logic_vector(NFFT - 1 downto 0) := (others => '0');
  signal psa_l_hi_i : std_logic_vector(NFFT - 1 downto 0) := (others => '0');
  signal psa_w_lo_i : std_logic_vector(NFFT - 1 downto 0) := (others => '0');
  signal psa_w_hi_i : std_logic_vector(NFFT - 1 downto 0) := (others => '0');

  signal result_valid_o : std_logic;
  signal psa_l_o        : std_logic_vector(ACC_WIDTH - 1 downto 0);
  signal psa_w_o        : std_logic_vector(ACC_WIDTH - 1 downto 0);

  signal test_count : integer := 0;
  signal fail_count : integer := 0;

  -- The testbench's OWN reversal, written independently of the package's.
  function tb_bit_reverse(v : integer; width : integer) return integer is
    variable r : integer := 0;
    variable x : integer := v;
  begin
    for i in 0 to width - 1 loop
      r := r * 2 + (x mod 2);
      x := x / 2;
    end loop;
    return r;
  end function tb_bit_reverse;

begin

  clk_i <= not clk_i after CLK_PERIOD / 2;

  uut : entity work.bin_accumulator
    generic map (FFT_LENGTH => FFT_LENGTH, DATA_WIDTH => DATA_WIDTH, ACC_WIDTH => ACC_WIDTH)
    port map (
      clk_i          => clk_i,
      rstn_i         => rstn_i,
      s_valid_i      => s_valid_i,
      s_re_i         => s_re_i,
      s_im_i         => s_im_i,
      s_last_i       => s_last_i,
      psa_l_lo_i     => psa_l_lo_i,
      psa_l_hi_i     => psa_l_hi_i,
      psa_w_lo_i     => psa_w_lo_i,
      psa_w_hi_i     => psa_w_hi_i,
      result_valid_o => result_valid_o,
      psa_l_o        => psa_l_o,
      psa_w_o        => psa_w_o
    );

  stim : process

    -- Magnitude assigned to bin k by the stimulus. k+1 so no bin is zero and every bin is
    -- distinguishable from its neighbours -- a window off by one bin changes the sum.
    function mag_of_bin(k : integer) return integer is
    begin
      return k + 1;
    end function mag_of_bin;

    procedure set_windows(l_lo, l_hi, w_lo, w_hi : integer) is
    begin
      psa_l_lo_i <= std_logic_vector(to_unsigned(l_lo, NFFT));
      psa_l_hi_i <= std_logic_vector(to_unsigned(l_hi, NFFT));
      psa_w_lo_i <= std_logic_vector(to_unsigned(w_lo, NFFT));
      psa_w_hi_i <= std_logic_vector(to_unsigned(w_hi, NFFT));
      wait until rising_edge(clk_i);
    end procedure set_windows;

    -- Drives one full frame in BEAT order. Beat n carries bin tb_bit_reverse(n).
    -- `negate` puts the magnitude in the negative half of the range to exercise the absolute
    -- value; the expected sums are identical either way, which is the point.
    procedure drive_frame(negate : boolean) is
      variable bin : integer;
      variable m   : integer;
    begin
      for n in 0 to NBINS - 1 loop
        bin := tb_bit_reverse(n, NFFT);
        m   := mag_of_bin(bin);
        if negate then
          s_re_i <= std_logic_vector(to_signed(-m, DATA_WIDTH));
        else
          s_re_i <= std_logic_vector(to_signed(m, DATA_WIDTH));
        end if;
        s_im_i    <= (others => '0');
        s_valid_i <= '1';
        if n = NBINS - 1 then
          s_last_i <= '1';
        else
          s_last_i <= '0';
        end if;
        wait until rising_edge(clk_i);
      end loop;
      s_valid_i <= '0';
      s_last_i  <= '0';
      wait until rising_edge(clk_i);
    end procedure drive_frame;

    function expected_sum(lo, hi : integer) return integer is
      variable acc : integer := 0;
    begin
      for k in lo to hi loop
        acc := acc + mag_of_bin(k);
      end loop;
      return acc;
    end function expected_sum;

    procedure check(name : string; ok : boolean) is
    begin
      test_count <= test_count + 1;
      wait until rising_edge(clk_i);
      if ok then
        report "  PASS: " & name;
      else
        fail_count <= fail_count + 1;
        report "  Test '" & name & "' FAILED" severity error;
      end if;
    end procedure check;

    variable got_l, got_w, exp_l, exp_w : integer;

  begin
    rstn_i <= '0';
    for i in 0 to 4 loop
      wait until rising_edge(clk_i);
    end loop;
    rstn_i <= '1';
    wait until rising_edge(clk_i);

    ---------------------------------------------------------------------------
    report "=== Test: project's real windows, bins 1-25 and 1-90 ===";
    set_windows(1, 25, 1, 90);
    drive_frame(false);
    got_l := to_integer(unsigned(psa_l_o));
    got_w := to_integer(unsigned(psa_w_o));
    exp_l := expected_sum(1, 25);   -- 350
    exp_w := expected_sum(1, 90);   -- 4185
    check("psa_l over bins 1..25 (got " & integer'image(got_l) & ", expected "
          & integer'image(exp_l) & ")", got_l = exp_l);
    check("psa_w over bins 1..90 (got " & integer'image(got_w) & ", expected "
          & integer'image(exp_w) & ")", got_w = exp_w);

    ---------------------------------------------------------------------------
    report "=== Test: absolute value -- negated input gives the same sums ===";
    drive_frame(true);
    got_l := to_integer(unsigned(psa_l_o));
    check("negative bins accumulate by magnitude (got " & integer'image(got_l) & ")",
          got_l = exp_l);

    ---------------------------------------------------------------------------
    report "=== Test: bounds are inclusive at both ends ===";
    -- A single-bin window must capture exactly that bin and nothing either side of it.
    set_windows(7, 7, 100, 100);
    drive_frame(false);
    got_l := to_integer(unsigned(psa_l_o));
    got_w := to_integer(unsigned(psa_w_o));
    check("window [7,7] captures exactly bin 7 (got " & integer'image(got_l) & ", expected "
          & integer'image(mag_of_bin(7)) & ")", got_l = mag_of_bin(7));
    check("window [100,100] captures exactly bin 100 (got " & integer'image(got_w) & ")",
          got_w = mag_of_bin(100));

    ---------------------------------------------------------------------------
    report "=== Test: DC is included when the window asks for it ===";
    -- Bin 0 is excluded by configuration, not by hardware -- prove the hardware has no special case.
    set_windows(0, 0, 0, 1023);
    drive_frame(false);
    got_l := to_integer(unsigned(psa_l_o));
    got_w := to_integer(unsigned(psa_w_o));
    check("window [0,0] captures DC (got " & integer'image(got_l) & ", expected "
          & integer'image(mag_of_bin(0)) & ")", got_l = mag_of_bin(0));
    check("window [0,1023] captures the whole spectrum (got " & integer'image(got_w)
          & ", expected " & integer'image(expected_sum(0, 1023)) & ")",
          got_w = expected_sum(0, 1023));

    ---------------------------------------------------------------------------
    report "=== Test: consecutive frames are independent ===";
    set_windows(1, 25, 1, 90);
    drive_frame(false);
    drive_frame(false);
    got_l := to_integer(unsigned(psa_l_o));
    check("second frame does not accumulate onto the first (got " & integer'image(got_l)
          & ", expected " & integer'image(exp_l) & ")", got_l = exp_l);

    ---------------------------------------------------------------------------
    report "=== Test: most-negative input does not overflow its own absolute value ===";
    -- abs(-32768) needs 16 unsigned bits; negating in place inside 16 signed bits would wrap it
    -- straight back to -32768 and the magnitude would come out as a huge wrong number.
    set_windows(3, 3, 3, 3);
    for n in 0 to NBINS - 1 loop
      if tb_bit_reverse(n, NFFT) = 3 then
        s_re_i <= std_logic_vector(to_signed(-32768, DATA_WIDTH));
        s_im_i <= std_logic_vector(to_signed(-32768, DATA_WIDTH));
      else
        s_re_i <= (others => '0');
        s_im_i <= (others => '0');
      end if;
      s_valid_i <= '1';
      if n = NBINS - 1 then
        s_last_i <= '1';
      else
        s_last_i <= '0';
      end if;
      wait until rising_edge(clk_i);
    end loop;
    s_valid_i <= '0';
    s_last_i  <= '0';
    wait until rising_edge(clk_i);
    wait until rising_edge(clk_i);
    got_l := to_integer(unsigned(psa_l_o));
    check("abs(-32768)+abs(-32768) = 65536 (got " & integer'image(got_l) & ")", got_l = 65536);

    ---------------------------------------------------------------------------
    wait until rising_edge(clk_i);
    report "=== " & integer'image(test_count) & " tests run, " & integer'image(fail_count)
           & " failed ===";
    if fail_count = 0 then
      report "TEST PASSED";
    else
      report "TEST FAILED" severity error;
    end if;
    wait;
  end process stim;

end architecture sim;
