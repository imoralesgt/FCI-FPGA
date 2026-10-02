-- Self-checking testbench for trace_tagger: frames of several lengths, each with its own TUSER,
-- under random input gaps and random output backpressure. Every output frame must be the input
-- samples unchanged followed by the four tag half-words (LSB first), with TLAST only on the last
-- tag beat.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

entity trace_tagger_tb is
end entity trace_tagger_tb;

architecture sim of trace_tagger_tb is

  constant CLK_PERIOD : time := 10 ns;
  type int_arr_t is array (natural range <>) of integer;
  constant LENGTHS : int_arr_t := (1, 2, 5, 64, 257);

  signal clk_i  : std_logic := '0';
  signal rstn_i : std_logic := '0';

  signal s_tdata  : std_logic_vector(15 downto 0) := (others => '0');
  signal s_tuser  : std_logic_vector(63 downto 0) := (others => '0');
  signal s_tlast  : std_logic := '0';
  signal s_tvalid : std_logic := '0';
  signal s_tready : std_logic;
  signal m_tdata  : std_logic_vector(15 downto 0);
  signal m_tlast  : std_logic;
  signal m_tvalid : std_logic;
  signal m_tready : std_logic := '0';

  signal done : boolean := false;

  function tag_of(f : integer) return std_logic_vector is
  begin
    -- Distinct half-words, with bit 63 set on odd frames (the pile-up flag position).
    return std_logic_vector(to_unsigned(f mod 2, 1)) & std_logic_vector(to_unsigned(16#123# + f, 15))
           & std_logic_vector(to_unsigned(16#4567# + f, 16))
           & std_logic_vector(to_unsigned(16#89AB# + f, 16))
           & std_logic_vector(to_unsigned(16#CDEF# - f, 16));
  end function;

begin

  clk_i <= not clk_i after CLK_PERIOD / 2;

  dut : entity work.trace_tagger
    port map (
      clk_i => clk_i, rstn_i => rstn_i,
      s_axis_tdata => s_tdata, s_axis_tuser => s_tuser, s_axis_tlast => s_tlast,
      s_axis_tvalid => s_tvalid, s_axis_tready => s_tready,
      m_axis_tdata => m_tdata, m_axis_tlast => m_tlast,
      m_axis_tvalid => m_tvalid, m_axis_tready => m_tready
    );

  source : process
    variable seed1, seed2 : positive := 7;
    variable r : real;
    variable i : integer;
  begin
    rstn_i <= '0';
    for k in 0 to 4 loop wait until rising_edge(clk_i); end loop;
    rstn_i <= '1';
    for f in LENGTHS'range loop
      i := 0;
      while i < LENGTHS(f) loop
        uniform(seed1, seed2, r);
        if r < 0.3 then
          s_tvalid <= '0';                 -- input gap
          wait until rising_edge(clk_i);
        else
          s_tvalid <= '1';
          s_tdata  <= std_logic_vector(to_unsigned(f * 1000 + i, 16));
          s_tuser  <= tag_of(f);
          if i = LENGTHS(f) - 1 then s_tlast <= '1'; else s_tlast <= '0'; end if;
          wait until rising_edge(clk_i) and s_tready = '1';
          i := i + 1;
        end if;
      end loop;
      s_tvalid <= '0';
      s_tlast  <= '0';
    end loop;
    wait;
  end process;

  backpressure : process
    variable seed1, seed2 : positive := 11;
    variable r : real;
  begin
    wait until rising_edge(clk_i);
    uniform(seed1, seed2, r);
    if r < 0.35 then m_tready <= '0'; else m_tready <= '1'; end if;
    if done then wait; end if;
  end process;

  sink : process
    variable fails : integer := 0;
    variable n     : integer;
    variable exp   : std_logic_vector(15 downto 0);
    variable tg    : std_logic_vector(63 downto 0);
  begin
    for f in LENGTHS'range loop
      n  := 0;
      tg := tag_of(f);
      loop
        wait until rising_edge(clk_i) and m_tvalid = '1' and m_tready = '1';
        if n < LENGTHS(f) then
          exp := std_logic_vector(to_unsigned(f * 1000 + n, 16));
        else
          exp := tg(16 * (n - LENGTHS(f)) + 15 downto 16 * (n - LENGTHS(f)));
        end if;
        if m_tdata /= exp then
          fails := fails + 1;
          report "FAIL frame " & integer'image(f) & " beat " & integer'image(n) & ": got "
                 & integer'image(to_integer(unsigned(m_tdata))) & " expected "
                 & integer'image(to_integer(unsigned(exp))) severity error;
        end if;
        if (m_tlast = '1') /= (n = LENGTHS(f) + 3) then
          fails := fails + 1;
          report "FAIL frame " & integer'image(f) & " TLAST wrong at beat " & integer'image(n)
            severity error;
        end if;
        n := n + 1;
        exit when m_tlast = '1' or n > LENGTHS(f) + 8;
      end loop;
      report "frame " & integer'image(f) & ": " & integer'image(n) & " beats";
    end loop;
    done <= true;
    if fails = 0 then report "TEST PASSED"; else report "TEST FAILED" severity error; end if;
    std.env.stop;
  end process;

end architecture sim;
