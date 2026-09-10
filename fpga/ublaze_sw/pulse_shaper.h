/**
 * @file pulse_shaper.h
 * @brief Driver for pulse_shaper_core (fpga/rtl/pulse_shaper_core) -- Jordanov-Knoll recursive
 *        trapezoidal filter, the spectroscopy energy channel (replaces psd_core's former
 *        single-sample raw-peak estimate).
 *
 * Same shape as psd.h/psd.c on purpose: one result-FIFO-backed core, peeked/popped the same way,
 * paired by the same 64-bit timestamp acquisition.c already pairs psd_core and fci_sink on.
 * Register map mirrors pulse_shaper_axi4lite_regs.vhd; keep the two in sync if that map changes.
 */

#ifndef SRC_PULSE_SHAPER_H_
#define SRC_PULSE_SHAPER_H_

#include "xil_types.h"

/**
 * @brief One event's worth of shaped-amplitude output.
 *
 * amplitude is SIGNED: it is a saturating resize of the filter's internal accumulator (see
 * trapezoidal_filter.vhd), which can run negative for a pathological/misconfigured input the same
 * way psd_core's gate integrals can.
 */
typedef struct {
  s32 amplitude;  /**< Shaped-pulse plateau height -- the spectroscopy energy channel. Scales with
                    *   the configured peaking time (A0*peaking for a matched decay); GUI-side
                    *   energy calibration absorbs that scale factor the same way it already
                    *   absorbs the previous raw-peak channel's own ADC-code scale. */
  u64 timestamp;  /**< trigger_core's 64-bit cycle count at the moment this pulse fired. */
} PulseShaperResult;

/**
 * @brief Configures the filter's three shaping parameters and enables/disables shaping.
 *
 * @param base     pulse_shaper_core's AXI4-Lite base address.
 * @param peaking  Peaking (rise) time, in samples at 50 Msps. Hardware-clamped 10..128.
 * @param flat_top Flat-top length, in samples. Hardware-clamped 0..128 (0 is a valid "triangular,
 *                 no flat top" configuration, not an error).
 * @param decay    Pole-zero decay time constant, in samples -- match this to the detector's own
 *                 measured pulse decay tau for the flat-top plateau height to land at
 *                 amplitude*peaking with no drift. Hardware-clamped 2..300. This function also
 *                 computes 1/decay in Q2.16 fixed point and writes it to the core's internal
 *                 decay_recip register, which is what the filter's pole-zero correction actually
 *                 uses (see PULSE_SHAPER_DECAY_RECIP_OFFSET in registers.h) -- the two registers
 *                 are kept in lockstep here rather than left for a caller to derive separately.
 * @param enable   0 bypasses shaping entirely: the core reports the frame's raw single-sample peak
 *                 instead (the same quantity psd_core's now-removed peak field used to report),
 *                 useful as an A/B reference. 1 is the normal operating mode.
 */
void PulseShaper_Configure(u32 base, u32 peaking, u32 flat_top, u32 decay, u32 enable);

/**
 * @brief Computes 1/decay in Q2.16 fixed point, the value the core's decay_recip register wants.
 *
 * A plain software division (cheap, done once per configuration write -- not something the
 * per-sample datapath should ever compute itself; see trapezoidal_filter.vhd's header comment).
 * Shared between PulseShaper_Configure() and cli.c's shaper_set() so the two call sites that can
 * change `decay` (bulk configuration, and a single-field $SH write) can't derive it differently.
 *
 * @param decay Decay time constant in samples. Must be > 0 (caller's responsibility -- this
 *              function does not range-check against the core's own 2..300 hardware limits).
 * @return 1/decay as a Q2.16 signed fixed-point value, ready to write to
 *         PULSE_SHAPER_DECAY_RECIP_OFFSET.
 */
s32 PulseShaper_DecayRecip(u32 decay);

/**
 * @brief Pops one result if the FIFO is non-empty.
 * @param base pulse_shaper_core's AXI4-Lite base address.
 * @param out  Filled in on success; untouched otherwise.
 * @return 1 on success, 0 if the FIFO was empty.
 */
int PulseShaper_Pop(u32 base, PulseShaperResult *out);

/**
 * @brief Reads the FIFO head without popping.
 *
 * For pairing against psd_core/fci_sink's own FIFOs, where the decision to consume depends on
 * what the other two sides are holding (acquisition.c's Acq_PopPaired()).
 *
 * @param base pulse_shaper_core's AXI4-Lite base address.
 * @param out  Filled in on success; untouched otherwise.
 * @return 1 on success, 0 if the FIFO was empty.
 */
int PulseShaper_Peek(u32 base, PulseShaperResult *out);

/**
 * @brief Discards the FIFO head. Only meaningful after a successful PulseShaper_Peek().
 * @param base pulse_shaper_core's AXI4-Lite base address.
 */
void PulseShaper_Discard(u32 base);

/** @brief Events currently buffered. @param base pulse_shaper_core's AXI4-Lite base address. */
u32 PulseShaper_Level(u32 base);
/** @brief Events processed since the last clear. @param base pulse_shaper_core's AXI4-Lite base address. */
u32 PulseShaper_EventCount(u32 base);
/** @brief Sticky: a result was dropped because the FIFO was full. @param base pulse_shaper_core's AXI4-Lite base address. */
int PulseShaper_Overflowed(u32 base);
/** @brief Empties the FIFO and clears overflow and the counter. @param base pulse_shaper_core's AXI4-Lite base address. */
void PulseShaper_Clear(u32 base);

/**
 * @brief Sets the FIFO watermark register. Built for parity with psd_core/fci_core, but nothing
 * in firmware acts on pulse_shaper_core's irq_o -- see registers.h's PULSE_SHAPER_WATERMARK_OFFSET
 * comment for why it is not wired to an interrupt input at all.
 *
 * @param base  pulse_shaper_core's AXI4-Lite base address.
 * @param level FIFO level the register is set to; has no observable effect on firmware behavior.
 */
void PulseShaper_SetWatermark(u32 base, u32 level);

/** @brief Self-test. @param base pulse_shaper_core's AXI4-Lite base address. @return 1 on success, 0 on failure. */
int PulseShaper_SelfTest(u32 base);

#endif /* SRC_PULSE_SHAPER_H_ */
