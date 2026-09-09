-- Jordanov-Knoll recursive trapezoidal filter: turns a baseline-subtracted, exponentially-decaying
-- detector pulse into a trapezoid whose flat-top plateau height is proportional to true pulse
-- amplitude -- averaged over many samples rather than picked from one, and largely independent of
-- the pulse's own decay time via pole-zero cancellation (the `decay` parameter).
--
-- Reference: V.T. Jordanov, G.F. Knoll, "Digital synthesis of pulse shapes in real time for high
-- resolution radiation spectroscopy", NIM A 345 (1994) 337-345. Same filter, same three parameter
-- names (decay/pole-zero, peaking, flat-top) CAEN's own DPP-PHA firmware uses. Equations below
-- follow the recursive derivation in ICTP's "Digital Gamma-Ray Spectroscopy: Trapezoidal
-- Filtering" (Advances in Digital Signal Processing, May 2013), which reproduces Jordanov &
-- Knoll's own recursive forms explicitly -- cross-checked against a hand-derived, exact (not
-- Taylor-approximated) worked example before implementing, after an EARLIER version of this file
-- had the stages in the wrong order (see below) and got the plateau wrong by ~200x, then by ~40x.
--
-- THE STRUCTURE, and why an earlier attempt here got it backwards: pole-zero correction is applied
-- to the RAW trace FIRST, producing a corrected trace that behaves like a clean (non-decaying)
-- step; the double-difference and accumulation that actually form the trapezoid are applied to
-- THAT corrected trace, not to the raw samples with a correction bolted on afterward. Doing it
-- backwards (double-difference the raw samples, then try to add a per-sample correction term
-- before a second accumulation) does not converge to a flat plateau for a real decaying input --
-- the accumulator's residual decays at the SAME rate as the input itself rather than settling, and
-- summing that slowly-decaying tail over hundreds of remaining frame samples dominates the result.
-- This is exactly the failure mode two earlier revisions of this file hit in simulation.
--
-- Stage 1 -- pole-zero correction (one accumulator):
--   Pz[n] = Pz[n-1] + x[n-1]           (running sum of the PREVIOUS raw sample)
--   Tr'[n] = x[n] + Pz[n] * (1/M)      (M = decay time constant in samples)
--   1/M is a small runtime-variable fraction (M ranges 2..400), not something a per-sample fabric
--   divider should compute at 50 MHz, so firmware precomputes it: `decay_recip_i` is +1/M in
--   Q2.16 fixed point (RECIP_FRAC_BITS below), written by PulseShaper_Configure()/shaper_set()
--   whenever `decay` (in samples -- the CLI-visible register) is written. The multiply is one
--   behavioral signed op, `a * signed(b)`, the same idiom trigger_core/src/cfd_trigger.vhd already
--   uses for its own runtime-programmable multiply to infer DSP48 slices rather than fabric
--   multiply logic -- only a fixed-point right shift was added on top, not a division.
--
-- Stage 2 -- double moving-sum difference (non-recursive), on Tr' rather than on x directly:
--   d[n] = Tr'[n] - Tr'[n-k] - Tr'[n-l] + Tr'[n-k-l],  k = peaking, l = k + flat_top.
--   Built from three cascaded variable_delay taps (Tr'[n] -k-> Tr'[n-k] -flat_top-> Tr'[n-l]
--   -k-> Tr'[n-k-l]) rather than one line deep enough to reach k+l directly -- same total SRL
--   cost, but each instance's own generic only needs to reach this core's k/flat_top maxima (250
--   each), not their sum, and both K-delay instances share the same `peaking_i` value.
--
-- Stage 3 -- single accumulation:
--   S[n] = S[n-1] + d[n]
--   Because Tr' behaves like a clean step (that is the entire point of stage 1), this single
--   accumulation is enough on its own to produce an exactly flat plateau of height A0*peaking for
--   a matched decay -- the same result a plain double-difference-plus-single-accumulation gives
--   for a genuinely non-decaying step input (worked by hand, region by region, while debugging
--   this file: for 0<=n<k the sum ramps linearly to A0*k, holds flat for k<=n<l, ramps back to 0
--   for l<=n<k+l, and stays at 0 afterward -- exactly the desired trapezoid, no second accumulator
--   or extra correction term needed once the input has been pole-zero corrected first).
--
-- Per-frame reset. trigger_core's output is bursty (exactly `depth` beats per triggered event, not
-- a continuous stream -- see psd_core_top.vhd's header), so both the delay taps (variable_delay.vhd)
-- and this filter's own accumulators/tracker must start every frame from a known zero rather than
-- free-running across the idle gap between events. Reset/publish happens on s_last_i, mirroring
-- dual_gate_integrator.vhd's own re-arm-at-last-beat structure exactly.
--
-- Accumulator widths are generous rather than tightly bounded to a "well-behaved pulse returns to
-- baseline quickly" assumption, matching dual_gate_integrator.vhd's own stated reasoning for its
-- ACC_WIDTH. Pz[n] sums one raw sample per cycle (worst case: every sample at full deviation for
-- the whole frame) and S[n] sums one double-difference term per cycle -- both are single (not
-- double, unlike an earlier wrong version of this file) accumulators, so PZ_WIDTH=32 and
-- S_WIDTH=40 comfortably cover the pathological case with margin, matching psd_core's own
-- ACC_WIDTH=32 precedent. The published amplitude is a SATURATING resize of the running peak down
-- to 32 bits, matching this project's "saturate, never wrap" convention used throughout
-- (trigger_core's cfd_frac_o/depth_o, dual_gate_integrator's own former peak_o, etc.).
--
-- enable_i='0' bypasses the shaper: the peak tracker follows x[n] directly (sign-extended) instead
-- of S[n], via a mux ahead of the SAME tracker -- not a second code path -- so the core still
-- produces exactly one result per frame either way, which the 3-way FIFO pairing in firmware
-- (acquisition.c's Acq_PopPaired()) depends on.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.pulse_shaper_core_pkg.all;

entity trapezoidal_filter is
  generic (
    DATA_WIDTH : integer := 16; -- signed sample datapath, matching blr_core/trigger_core/psd_core
    K_MAX      : integer := 250; -- peaking-time hardware ceiling (spec range 10..250)
    M_MAX      : integer := 250; -- flat-top hardware ceiling (spec range 0..250)
    RECIP_BITS      : integer := 18; -- decay_recip_i width, signed Q2.16
    RECIP_FRAC_BITS : integer := 16; -- fractional bits of decay_recip_i
    ACC_WIDTH  : integer := 32
  );
  port (
    clk_i  : in std_logic;
    rstn_i : in std_logic;

    -- Framed sample stream in, bursty (see header). tready is not present: this core must never
    -- backpressure the lockstep broadcaster, same as psd_core/fci_core.
    s_valid_i : in std_logic;
    s_data_i  : in std_logic_vector(DATA_WIDTH - 1 downto 0);
    s_last_i  : in std_logic;

    peaking_i      : in std_logic_vector(clog2(K_MAX) - 1 downto 0);
    flat_top_i     : in std_logic_vector(clog2(M_MAX) - 1 downto 0);
    decay_recip_i  : in std_logic_vector(RECIP_BITS - 1 downto 0); -- +1/M, Q2.16, see header
    enable_i       : in std_logic;

    -- One pulse per completed frame, alongside the shaped-peak amplitude.
    result_valid_o : out std_logic;
    amplitude_o    : out std_logic_vector(ACC_WIDTH - 1 downto 0)
  );
end entity trapezoidal_filter;

architecture rtl of trapezoidal_filter is

  constant PZ_WIDTH   : integer := 32; -- Stage 1 accumulator (running sum of x[n-1])
  constant TR_WIDTH    : integer := 20; -- Tr'[n]: x[n] plus a small correction, modest headroom
  constant DIFF_WIDTH   : integer := 24; -- d[n]: 4-term combine of TR_WIDTH values, with margin
  constant S_WIDTH        : integer := 40; -- Stage 3 accumulator

  signal x_prev : signed(DATA_WIDTH - 1 downto 0); -- x[n-1], see header's Pz recursion
  signal pz     : signed(PZ_WIDTH - 1 downto 0);
  signal s_acc  : signed(S_WIDTH - 1 downto 0);
  signal peak   : signed(S_WIDTH - 1 downto 0);

  signal next_pz_comb : signed(PZ_WIDTH - 1 downto 0); -- Pz[n], combinational (see process below)
  signal tr_comb : std_logic_vector(TR_WIDTH - 1 downto 0); -- Tr'[n], combinational -- NOT
  -- registered: an earlier version of this file registered Tr' here, which inserted an extra,
  -- unintended cycle of latency on top of the delay lines' own correct, intentional per-tap
  -- latency. That silently dropped each frame's last sample from the shaped result and let one
  -- stale sample from the previous frame leak into the next frame's first cycle -- caught by the
  -- "back-to-back frames don't leak" testbench case reading ~1000 (the previous pulse's own
  -- amplitude) instead of ~0 on the very next, otherwise-quiet frame.
  signal tr_k, tr_l, tr_kl : std_logic_vector(TR_WIDTH - 1 downto 0); -- delayed taps of Tr'
  signal clear_frame        : std_logic; -- pulses on s_last_i: clears delay taps for the NEXT frame

  constant PEAK_MIN : signed(S_WIDTH - 1 downto 0) := (S_WIDTH - 1 => '1', others => '0');

  -- Saturating resize, S_WIDTH signed -> OUT_WIDTH signed. Clamps rather than truncates: see the
  -- header comment on why a misconfigured `decay` must saturate visibly, not wrap.
  function sat_resize(v : signed; out_width : integer) return signed is
    variable max_out : signed(out_width - 1 downto 0) := (out_width - 1 => '0', others => '1');
    variable min_out  : signed(out_width - 1 downto 0) := (out_width - 1 => '1', others => '0');
  begin
    if v > resize(max_out, v'length) then
      return max_out;
    elsif v < resize(min_out, v'length) then
      return min_out;
    else
      return resize(v, out_width);
    end if;
  end function sat_resize;

begin

  clear_frame <= s_last_i;

  -- Stage 1, combinational: Pz[n] and Tr'[n] from the CURRENT register values (pz, x_prev) and
  -- this cycle's x[n] -- see tr_comb's own declaration for why this must NOT be registered.
  comb_stage1 : process (pz, x_prev, s_data_i, decay_recip_i)
    variable next_pz  : signed(PZ_WIDTH - 1 downto 0);
    variable prod_var : signed(PZ_WIDTH + RECIP_BITS - 1 downto 0);
    variable corr_var : signed(PZ_WIDTH - 1 downto 0);
  begin
    -- Pz[n] = Pz[n-1] + x[n-1] -- x_prev already holds x[n-1] from the previous cycle.
    next_pz := pz + resize(x_prev, PZ_WIDTH);

    -- Tr'[n] = x[n] + Pz[n] * (1/M), using the JUST-COMPUTED Pz[n] (matches the reference
    -- derivation's Tr'(n) = Tr(n) + Pz(n)/tau exactly -- Pz(n) already includes x(n-1)).
    prod_var := next_pz * signed(decay_recip_i);
    corr_var := resize(shift_right(prod_var, RECIP_FRAC_BITS), PZ_WIDTH);

    next_pz_comb <= next_pz;
    tr_comb <= std_logic_vector(sat_resize(resize(signed(s_data_i), PZ_WIDTH) + corr_var,
                                            TR_WIDTH));
  end process comb_stage1;

  -- Stage 1 registers: pz/x_prev simply latch what comb_stage1 just computed from them.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        x_prev <= (others => '0');
        pz     <= (others => '0');
      elsif s_valid_i = '1' then
        if clear_frame = '1' then
          pz     <= (others => '0');
          x_prev <= (others => '0');
        else
          pz     <= next_pz_comb;
          x_prev <= signed(s_data_i);
        end if;
      end if;
    end if;
  end process;

  u_delay_k1 : entity work.variable_delay
    generic map (DATA_WIDTH => TR_WIDTH, MAX_DELAY => K_MAX)
    port map (clk_i => clk_i, rstn_i => rstn_i, clear_i => clear_frame, en_i => s_valid_i,
              delay_sel_i => peaking_i, data_i => tr_comb, data_o => tr_k);

  u_delay_m : entity work.variable_delay
    generic map (DATA_WIDTH => TR_WIDTH, MAX_DELAY => M_MAX)
    port map (clk_i => clk_i, rstn_i => rstn_i, clear_i => clear_frame, en_i => s_valid_i,
              delay_sel_i => flat_top_i, data_i => tr_k, data_o => tr_l);

  u_delay_k2 : entity work.variable_delay
    generic map (DATA_WIDTH => TR_WIDTH, MAX_DELAY => K_MAX)
    port map (clk_i => clk_i, rstn_i => rstn_i, clear_i => clear_frame, en_i => s_valid_i,
              delay_sel_i => peaking_i, data_i => tr_l, data_o => tr_kl);

  process (clk_i)
    variable d_var    : signed(DIFF_WIDTH - 1 downto 0);
    variable next_s   : signed(S_WIDTH - 1 downto 0);
    variable tracked  : signed(S_WIDTH - 1 downto 0);
    variable next_peak : signed(S_WIDTH - 1 downto 0);
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        s_acc          <= (others => '0');
        peak           <= PEAK_MIN;
        result_valid_o <= '0';
        amplitude_o    <= (others => '0');
      else
        result_valid_o <= '0';

        if s_valid_i = '1' then
          d_var := resize(signed(tr_comb), DIFF_WIDTH) - resize(signed(tr_k), DIFF_WIDTH)
                   - resize(signed(tr_l), DIFF_WIDTH) + resize(signed(tr_kl), DIFF_WIDTH);

          next_s := s_acc + resize(d_var, S_WIDTH);

          if enable_i = '1' then
            tracked := next_s;
          else
            tracked := resize(signed(s_data_i), S_WIDTH);
          end if;

          if tracked > peak then
            next_peak := tracked;
          else
            next_peak := peak;
          end if;

          if s_last_i = '1' then
            -- Frame complete: publish the shaped-peak amplitude and re-arm for the next event
            -- (delay taps and the stage-1 accumulator re-arm via clear_frame, same s_last_i).
            amplitude_o    <= std_logic_vector(sat_resize(next_peak, ACC_WIDTH));
            result_valid_o <= '1';
            s_acc          <= (others => '0');
            peak           <= PEAK_MIN;
          else
            s_acc <= next_s;
            peak  <= next_peak;
          end if;
        end if;
      end if;
    end if;
  end process;

end architecture rtl;
