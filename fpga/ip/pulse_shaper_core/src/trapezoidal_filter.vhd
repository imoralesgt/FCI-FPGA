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
-- Clock: this core lives in the `clk_cpu_dpp` domain (see fci_bd.tcl's clk_wiz_0,
-- CLKOUT2_REQUESTED_OUT_FREQ=150), not the 50 MHz `clk_adc` domain trigger_core/blr_core run in --
-- 150 MHz gives only 6.667 ns per cycle, a much tighter combinational budget than the 20 ns an
-- earlier revision of this file's own comments mistakenly assumed (see the pipelining notes
-- below). This does NOT change what a `decay`/`peaking`/`flat_top` register COUNT means physically
-- -- s_valid_i itself only pulses in step with the 50 Msps ADC stream (through the CDC upstream of
-- the AXI-Stream broadcaster all three lockstep cores share), so one count is still one 20 ns
-- sample period, just processed by fabric that ticks roughly 3x faster than samples arrive --
-- comfortable room for the multi-cycle pipeline below without costing any per-sample throughput.
--
-- Stage 1 -- pole-zero correction:
--   Pz[n] = Pz[n-1] + x[n-1]           (running sum of the PREVIOUS raw sample, one accumulator)
--   Tr'[n] = x[n] + Pz[n] * (1/M)      (M = decay time constant in samples)
--   1/M is a small runtime-variable fraction (M ranges 2..300), not something a per-sample fabric
--   divider should compute every cycle, so firmware precomputes it: `decay_recip_i` is +1/M in
--   Q2.16 fixed point (RECIP_FRAC_BITS below), written by PulseShaper_Configure()/shaper_set()
--   whenever `decay` (in samples -- the CLI-visible register) is written.
--
--   The Pz[n]*recip multiply is PIPELINED across two register stages (mult_a_reg/mult_b_reg, then
--   prod_reg), matching a DSP48's own AREG/BREG/MREG pipeline registers, rather than one fully
--   combinational expression. An earlier revision described it combinationally (the same
--   behavioral-multiply idiom trigger_core/src/cfd_trigger.vhd uses, which infers a DSP48 slice
--   just fine) -- that DOES infer a DSP48, but with every one of its own internal pipeline
--   registers left disabled (confirmed in the post-synthesis DSP mapping report: AREG=0, BREG=0,
--   MREG=0, PREG=0), forcing the entire 32x18 multiply plus everything chained after it through
--   combinationally in one cycle. Measured result: -18.818 ns worst negative slack against the
--   6.667 ns period (i.e. the actual path was nearly 4x the period). Registering the multiply's
--   operands and product costs two extra cycles of latency (Pz[n]/M isn't ready until n+2), not
--   two extra cycles of THROUGHPUT -- a new sample is still accepted every cycle -- and does not
--   change Tr'[n]'s VALUE for a given n, only when it becomes available, since the multiply's
--   result feeds only the Tr' correction term, never Pz's own recursive update (pz/x_prev keep
--   accumulating on the untouched, original timeline below). That first fix alone brought slack to
--   -9.545 ns, still violating -- the remaining, still-combinational Tr' saturate-add plus the
--   entire stage 2/3 double-difference/accumulate/compare/saturate chain (see PIPELINE STAGES 3-4
--   below) was still too deep for 6.667 ns on its own.
--
--   Every pipeline cut has to be carried consistently by everything DOWNSTREAM of the cut, or the
--   delay taps and the double-difference would desync from what Tr' is actually presenting each
--   cycle -- valid_d1..valid_d4/last_d1..last_d4 (a plain shift register of s_valid_i/s_last_i,
--   unrelated to Pz/Tr' themselves) are what each stage and the delay lines' en_i key off instead
--   of the raw stream signals, and x_pipe1..x_pipe4 carry x[n] the same number of cycles so it is
--   always added to / compared against a correctly time-matched partner once each pipeline stage
--   catches up.
--
-- PIPELINE STAGES 3-4 (added after the first, multiply-only pipelining attempt still left slack at
-- -9.545 ns): Tr'[n] itself (tr_reg, stage 3) and the double-difference d[n] (d_reg, stage 4) are
-- now each their own registered stage, rather than both being combinational logic feeding straight
-- into stage 3's accumulate/compare/saturate in the same cycle the multiply pipeline produced
-- prod_reg. Splitting one long chain (Tr's saturating add, then the 4-term difference, then the
-- accumulate, compare, and final saturate) into three registered segments is what actually fits
-- each segment under 6.667 ns; a single one of those steps alone (e.g. just the accumulate+compare)
-- was not the bottleneck, the SUM of all of them evaluated combinationally in one cycle was.
--
-- Stage 2 -- double moving-sum difference (non-recursive), on Tr' rather than on x directly:
--   d[n] = Tr'[n] - Tr'[n-k] - Tr'[n-l] + Tr'[n-k-l],  k = peaking, l = k + flat_top.
--   Built from three cascaded variable_delay taps (Tr'[n] -k-> Tr'[n-k] -flat_top-> Tr'[n-l]
--   -k-> Tr'[n-k-l]) rather than one line deep enough to reach k+l directly -- same total resource
--   cost, but each instance's own generic only needs to reach this core's k/flat_top maxima (128
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
-- a continuous stream -- see psd_core_top.vhd's header), so this filter's own accumulators/tracker
-- must start every frame from a known zero. Pz[n]'s own accumulator resets on the RAW s_last_i
-- (it lives on the original, unlagged timeline); the final accumulate/compare/publish stage resets
-- on last_d4, the four-cycle-delayed signal its own inputs (d_reg, x_pipe4) are aligned to. The
-- delay taps (variable_delay.vhd) are themselves genuinely free-running (no per-frame clear at all
-- -- see that file's own header for why two earlier attempts at clearing them made no difference
-- to LUT cost either way).
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
    K_MAX      : integer := 128; -- peaking-time hardware ceiling (spec range 10..128 -- narrowed
                                 -- from an original 10..250 specifically to shrink this core's
                                 -- own delay-line depth; see variable_delay.vhd's header)
    M_MAX      : integer := 128; -- flat-top hardware ceiling (spec range 0..128, same reason)
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
  constant PROD_WIDTH      : integer := PZ_WIDTH + RECIP_BITS; -- Pz[n]*recip product width

  signal x_prev : signed(DATA_WIDTH - 1 downto 0); -- x[n-1], see header's Pz recursion
  signal pz     : signed(PZ_WIDTH - 1 downto 0);
  signal s_acc  : signed(S_WIDTH - 1 downto 0);
  signal peak   : signed(S_WIDTH - 1 downto 0);

  signal next_pz_comb : signed(PZ_WIDTH - 1 downto 0); -- Pz[n], combinational (feeds pz's own
  -- update below AND stage 1 of the multiply pipeline -- never fed back from the pipeline itself)

  -- Two-stage multiply pipeline (matches a DSP48's AREG/BREG then MREG): see header for why.
  signal mult_a_reg : signed(PZ_WIDTH - 1 downto 0);
  signal mult_b_reg : signed(RECIP_BITS - 1 downto 0);
  signal prod_reg   : signed(PROD_WIDTH - 1 downto 0);

  -- x[n] carried the same number of cycles as the rest of the pipeline (4 stages total now), so it
  -- can be added to / compared against a time-matched partner at each stage boundary.
  signal x_pipe1, x_pipe2, x_pipe3, x_pipe4 : signed(DATA_WIDTH - 1 downto 0);

  -- Plain four-deep shift registers of the stream's own valid/last flags, unconditional (never
  -- gated by themselves) -- tracks which pipeline stage currently holds a genuine sample versus
  -- pipeline-fill/drain filler, exactly the way any fixed-latency pipeline's valid bit must.
  signal valid_d1, valid_d2, valid_d3, valid_d4 : std_logic;
  signal last_d1, last_d2, last_d3, last_d4     : std_logic;

  signal tr_comb : std_logic_vector(TR_WIDTH - 1 downto 0); -- Tr'[n], combinational from
  -- prod_reg/x_pipe2 -- registered into tr_reg (stage 3) below rather than being fed straight into
  -- the delay taps and the difference stage in the same cycle it was computed, which is what still
  -- left slack at -9.545 ns after only the multiply itself was pipelined.
  signal tr_reg : std_logic_vector(TR_WIDTH - 1 downto 0); -- Tr'[n], stage-3 registered
  signal tr_k, tr_l, tr_kl : std_logic_vector(TR_WIDTH - 1 downto 0); -- delayed taps of tr_reg

  signal d_var_comb : signed(DIFF_WIDTH - 1 downto 0); -- d[n], combinational from tr_reg/tr_k/
  -- tr_l/tr_kl -- registered into d_reg (stage 4) below for the same reason tr_comb is registered
  -- into tr_reg rather than left combinational into the accumulate/compare/saturate that follows.
  signal d_reg : signed(DIFF_WIDTH - 1 downto 0); -- d[n], stage-4 registered

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

  -- Pipeline valid/last tracking: unconditional, every cycle -- see the signals' own declaration.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        valid_d1 <= '0';
        valid_d2 <= '0';
        valid_d3 <= '0';
        valid_d4 <= '0';
        last_d1  <= '0';
        last_d2  <= '0';
        last_d3  <= '0';
        last_d4  <= '0';
      else
        valid_d1 <= s_valid_i;
        valid_d2 <= valid_d1;
        valid_d3 <= valid_d2;
        valid_d4 <= valid_d3;
        last_d1  <= s_last_i;
        last_d2  <= last_d1;
        last_d3  <= last_d2;
        last_d4  <= last_d3;
      end if;
    end if;
  end process;

  -- Pz[n] = Pz[n-1] + x[n-1], combinational -- cheap (one adder), never part of the multiply
  -- pipeline, so it is not what needed pipelining in the first place.
  next_pz_comb <= pz + resize(x_prev, PZ_WIDTH);

  -- Stage 1's own accumulator: unchanged from before pipelining was added, still keyed to the
  -- RAW, unlagged s_valid_i/s_last_i -- this is the SOURCE the pipeline reads from, not a
  -- consumer of it, so it stays on the original timeline.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        x_prev <= (others => '0');
        pz     <= (others => '0');
      elsif s_valid_i = '1' then
        if s_last_i = '1' then
          pz     <= (others => '0');
          x_prev <= (others => '0');
        else
          pz     <= next_pz_comb;
          x_prev <= signed(s_data_i);
        end if;
      end if;
    end if;
  end process;

  -- Multiply pipeline stage 1: register the operands (matches DSP48 AREG/BREG). Gated by
  -- s_valid_i, the enable for the data next_pz_comb/s_data_i were themselves computed from.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        mult_a_reg <= (others => '0');
        mult_b_reg <= (others => '0');
        x_pipe1    <= (others => '0');
      elsif s_valid_i = '1' then
        mult_a_reg <= next_pz_comb;
        mult_b_reg <= signed(decay_recip_i);
        x_pipe1    <= signed(s_data_i);
      end if;
    end if;
  end process;

  -- Multiply pipeline stage 2: register the product (matches DSP48 MREG/PREG). Gated by
  -- valid_d1 -- mult_a_reg/mult_b_reg/x_pipe1 are only freshly meaningful when it is set.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        prod_reg <= (others => '0');
        x_pipe2  <= (others => '0');
      elsif valid_d1 = '1' then
        prod_reg <= mult_a_reg * mult_b_reg;
        x_pipe2  <= x_pipe1;
      end if;
    end if;
  end process;

  -- Tr'[n] = x[n] + Pz[n]*(1/M), combinational from the now-time-matched prod_reg/x_pipe2 (both
  -- reflect the same original sample index once valid_d2 is set).
  tr_comb <= std_logic_vector(
    sat_resize(resize(x_pipe2, PZ_WIDTH)
               + resize(shift_right(prod_reg, RECIP_FRAC_BITS), PZ_WIDTH), TR_WIDTH));

  -- Pipeline stage 3: register Tr'[n] itself, rather than feeding the still-combinational tr_comb
  -- straight into the delay taps and the difference stage -- see header.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        tr_reg  <= (others => '0');
        x_pipe3 <= (others => '0');
      elsif valid_d2 = '1' then
        tr_reg  <= tr_comb;
        x_pipe3 <= x_pipe2;
      end if;
    end if;
  end process;

  u_delay_k1 : entity work.variable_delay
    generic map (DATA_WIDTH => TR_WIDTH, MAX_DELAY => K_MAX)
    port map (clk_i => clk_i, rstn_i => rstn_i, en_i => valid_d3,
              delay_sel_i => peaking_i, data_i => tr_reg, data_o => tr_k);

  u_delay_m : entity work.variable_delay
    generic map (DATA_WIDTH => TR_WIDTH, MAX_DELAY => M_MAX)
    port map (clk_i => clk_i, rstn_i => rstn_i, en_i => valid_d3,
              delay_sel_i => flat_top_i, data_i => tr_k, data_o => tr_l);

  u_delay_k2 : entity work.variable_delay
    generic map (DATA_WIDTH => TR_WIDTH, MAX_DELAY => K_MAX)
    port map (clk_i => clk_i, rstn_i => rstn_i, en_i => valid_d3,
              delay_sel_i => peaking_i, data_i => tr_l, data_o => tr_kl);

  -- d[n] = Tr'[n] - Tr'[n-k] - Tr'[n-l] + Tr'[n-k-l], combinational from tr_reg/tr_k/tr_l/tr_kl
  -- (all reflect the same original sample index once valid_d3 is set).
  d_var_comb <= resize(signed(tr_reg), DIFF_WIDTH) - resize(signed(tr_k), DIFF_WIDTH)
                - resize(signed(tr_l), DIFF_WIDTH) + resize(signed(tr_kl), DIFF_WIDTH);

  -- Pipeline stage 4: register d[n] itself, rather than feeding the still-combinational
  -- d_var_comb straight into the accumulate/compare/saturate below in the same cycle -- see
  -- header.
  process (clk_i)
  begin
    if rising_edge(clk_i) then
      if rstn_i = '0' then
        d_reg   <= (others => '0');
        x_pipe4 <= (others => '0');
      elsif valid_d3 = '1' then
        d_reg   <= d_var_comb;
        x_pipe4 <= x_pipe3;
      end if;
    end if;
  end process;

  process (clk_i)
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

        if valid_d4 = '1' then
          next_s := s_acc + resize(d_reg, S_WIDTH);

          if enable_i = '1' then
            tracked := next_s;
          else
            tracked := resize(x_pipe4, S_WIDTH);
          end if;

          if tracked > peak then
            next_peak := tracked;
          else
            next_peak := peak;
          end if;

          if last_d4 = '1' then
            -- Frame complete: publish the shaped-peak amplitude and re-arm for the next event.
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
