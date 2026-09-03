-- Dual-window charge integrator: the CAEN-style ENERGY / ENERGY_SHORT pair.
--
-- Consumes one framed trace per event from the broadcaster (trigger_core emits exactly depth beats
-- with tlast on the final one) and accumulates the baseline-subtracted sample value over two
-- windows that share a start point:
--
--     frame index:  0 ............ pre_trigger ............................ depth-1
--                              |<-- pre_gate -->|
--                   ...........[================ long_gate ==============]......
--                              [=== short_gate ===]
--
-- Both gates open at pre_trigger - pre_gate. The short gate captures the prompt component, the long
-- gate captures prompt plus delayed; their ratio is the discrimination parameter, the same quantity
-- CAEN digitizers report as ENERGY_SHORT and ENERGY (long).
--
-- Baseline. Samples arrive SIGNED and already restored to zero by blr_core, so the charge integral
-- is just the sum of the samples themselves. baseline_ref_i survives as a residual-pedestal
-- correction, defaulting to 0: if the restorer leaves a small systematic offset it shows up as a
-- gate-length-proportional term in the integral, and this is the knob that removes it without
-- re-tuning the restorer. It is also what makes the integrator usable with blr_core bypassed,
-- which is the A/B comparison the BLR needs in order to prove it is doing something.
--
-- Accumulator width. Worst case is every sample at full deviation for the whole frame:
-- 2^15 * 4096 = 2^27 for a 16-bit signed sample, so 28 bits are needed and 32 are used -- both for
-- headroom and because a 32-bit result word is what the firmware and the CAEN convention expect.
-- Signed, because undershoot below baseline is real and must subtract rather than wrap.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.psd_core_pkg.all;

entity dual_gate_integrator is
  generic (
    DATA_WIDTH : integer := 16; -- signed sample datapath, matching blr_core/trigger_core
    MAX_DEPTH  : integer := 4096;
    ACC_WIDTH  : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- Framed sample stream in. tready is not present on purpose: this core must never backpressure
    -- (see psd_core_top's header).
    s_valid_i : in std_logic;
    s_data_i  : in std_logic_vector(DATA_WIDTH - 1 downto 0);
    s_last_i  : in std_logic;

    baseline_ref_i : in std_logic_vector(DATA_WIDTH - 1 downto 0);
    pre_trigger_i  : in std_logic_vector(clog2(MAX_DEPTH) - 1 downto 0);
    pre_gate_i     : in std_logic_vector(clog2(MAX_DEPTH) - 1 downto 0);
    short_gate_i   : in std_logic_vector(clog2(MAX_DEPTH) - 1 downto 0);
    long_gate_i    : in std_logic_vector(clog2(MAX_DEPTH) - 1 downto 0);

    -- One pulse per completed frame, alongside the two integrals.
    result_valid_o : out std_logic;
    energy_short_o : out std_logic_vector(ACC_WIDTH - 1 downto 0);
    energy_long_o  : out std_logic_vector(ACC_WIDTH - 1 downto 0);
    peak_o         : out std_logic_vector(ACC_WIDTH - 1 downto 0)
  );
end entity dual_gate_integrator;

architecture rtl of dual_gate_integrator is

  constant IDX_WIDTH : integer := clog2(MAX_DEPTH);

  -- Most negative value representable in peak's width: the correct reset/re-arm value, not 0.
  -- A frame that never rises above baseline (all samples negative, e.g. undershoot with no real
  -- pulse) must still report its true maximum rather than a floor of 0, which would silently hide
  -- that the frame had no positive excursion at all.
  constant PEAK_MIN : signed(DATA_WIDTH downto 0) := (DATA_WIDTH => '1', others => '0');

  signal idx : unsigned(IDX_WIDTH - 1 downto 0); -- beat index within the current frame

  signal acc_short : signed(ACC_WIDTH - 1 downto 0);
  signal acc_long  : signed(ACC_WIDTH - 1 downto 0);
  signal peak      : signed(DATA_WIDTH downto 0);
  -- Running maximum of `dev` over the whole frame, not just the PSD gates: peak amplitude is a
  -- whole-pulse property (the spectroscopy energy channel), independent of where the short/long
  -- gates happen to sit.

begin

  process (clk_i)
    variable dev        : signed(DATA_WIDTH downto 0);
    variable gate_start : unsigned(IDX_WIDTH - 1 downto 0);
    variable short_end  : unsigned(IDX_WIDTH + 1 downto 0);
    variable long_end   : unsigned(IDX_WIDTH + 1 downto 0);
    variable in_short   : boolean;
    variable in_long    : boolean;
    variable next_short : signed(ACC_WIDTH - 1 downto 0);
    variable next_long  : signed(ACC_WIDTH - 1 downto 0);
    variable next_peak  : signed(DATA_WIDTH downto 0);
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        idx            <= (others => '0');
        acc_short      <= (others => '0');
        acc_long       <= (others => '0');
        peak           <= PEAK_MIN;
        result_valid_o <= '0';
        energy_short_o <= (others => '0');
        energy_long_o  <= (others => '0');
        peak_o         <= (others => '0');
      else
        result_valid_o <= '0';

        if s_valid_i = '1' then
          -- Gate start, clamped at 0: a pre_gate larger than pre_trigger would otherwise wrap and
          -- silently place the window at the far end of the frame.
          if unsigned(pre_gate_i) >= unsigned(pre_trigger_i) then
            gate_start := (others => '0');
          else
            gate_start := unsigned(pre_trigger_i) - unsigned(pre_gate_i);
          end if;

          short_end := resize(gate_start, IDX_WIDTH + 2) + resize(unsigned(short_gate_i), IDX_WIDTH + 2);
          long_end  := resize(gate_start, IDX_WIDTH + 2) + resize(unsigned(long_gate_i), IDX_WIDTH + 2);

          in_short := (idx >= gate_start) and (resize(idx, IDX_WIDTH + 2) < short_end);
          in_long  := (idx >= gate_start) and (resize(idx, IDX_WIDTH + 2) < long_end);

          dev := resize(signed(s_data_i), DATA_WIDTH + 1)
                 - resize(signed(baseline_ref_i), DATA_WIDTH + 1);

          if in_short then
            next_short := acc_short + resize(dev, ACC_WIDTH);
          else
            next_short := acc_short;
          end if;

          if in_long then
            next_long := acc_long + resize(dev, ACC_WIDTH);
          else
            next_long := acc_long;
          end if;

          if dev > peak then
            next_peak := dev;
          else
            next_peak := peak;
          end if;

          if s_last_i = '1' then
            -- Frame complete: publish both integrals and re-arm for the next event. The final beat
            -- is included in the totals above before they are emitted, so a gate that runs to the
            -- end of the frame loses no charge. peak_o resizes the (DATA_WIDTH+1)-bit running
            -- maximum up to ACC_WIDTH for uniformity with energy_short_o/energy_long_o -- same
            -- ACC_WIDTH-for-consistency choice those two already make despite not needing the full
            -- range either.
            energy_short_o <= std_logic_vector(next_short);
            energy_long_o  <= std_logic_vector(next_long);
            peak_o         <= std_logic_vector(resize(next_peak, ACC_WIDTH));
            result_valid_o <= '1';
            acc_short      <= (others => '0');
            acc_long       <= (others => '0');
            peak           <= PEAK_MIN;
            idx            <= (others => '0');
          else
            acc_short <= next_short;
            acc_long  <= next_long;
            peak      <= next_peak;
            idx       <= idx + 1;
          end if;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
