/**
 * @file psd.h
 * @brief Driver for psd_core (fpga/rtl/psd_core) -- CAEN-style dual-gate charge integration.
 *
 * Register map mirrors psd_axi4lite_regs.vhd; keep the two in sync if that map changes.
 */

#ifndef SRC_PSD_H_
#define SRC_PSD_H_

#include "xil_types.h"

/**
 * @brief One event's worth of PSD output.
 *
 * energy_short/energy_long/peak are SIGNED: undershoot below the baseline reference genuinely
 * subtracts, and treating them as unsigned would turn a small negative integral into a huge
 * positive one.
 */
typedef struct {
  s32 energy_short; /**< PSD short-gate charge integral. */
  s32 energy_long;  /**< PSD long-gate charge integral. */
  s32 peak;         /**< Max baseline-subtracted sample over the whole frame, signed for the same
                      *   undershoot reason as energy_short/energy_long. */
  u64 timestamp;    /**< trigger_core's 64-bit cycle count at the moment this pulse fired. */
} PsdResult;

/**
 * @brief Configures psd_core's gate geometry and residual-pedestal trim.
 *
 * @param base         psd_core's AXI4-Lite base address.
 * @param pre_trigger  MUST match trigger_core's delay register: it is where the trigger sits
 *                     inside the captured frame, and psd_core has no other way to know.
 * @param pre_gate     Gate geometry in samples at 50 Msps -- samples before the trigger included
 *                     in both gates.
 * @param short_gate   Short integration window length, in samples.
 * @param long_gate    Long integration window length, in samples.
 * @param baseline_ref SIGNED residual-pedestal trim, passed as u32 because that is what the
 *                     register write takes -- a negative trim must be written as its
 *                     two's-complement value in the low DATA_WIDTH bits, e.g.
 *                     Psd_Configure(..., (u32)(s32)-12). 0 is correct when fed by blr_core, which
 *                     restores the baseline to zero rather than to mid-scale. (An earlier version
 *                     of this comment said 8192; that belonged to the offset-binary datapath,
 *                     which is gone.)
 *
 * @note baseline_ref cannot correct the low-energy PSD pathology: a constant offset shifts El and
 *       Es by fixed multiples of the gate lengths, which cannot drive PSD negative at low energy.
 *       See docs/log 8d.
 */
void Psd_Configure(u32 base, u32 pre_trigger, u32 pre_gate, u32 short_gate, u32 long_gate,
                   u32 baseline_ref);

/**
 * @brief Pops one result if the FIFO is non-empty.
 * @param base psd_core's AXI4-Lite base address.
 * @param out  Filled in on success; untouched otherwise.
 * @return 1 on success, 0 if the FIFO was empty.
 */
int Psd_Pop(u32 base, PsdResult *out);

/**
 * @brief Reads the FIFO head without popping.
 *
 * For pairing against another core's FIFO, where the decision to consume depends on what the
 * other side is holding.
 *
 * @param base psd_core's AXI4-Lite base address.
 * @param out  Filled in on success; untouched otherwise.
 * @return 1 on success, 0 if the FIFO was empty.
 */
int Psd_Peek(u32 base, PsdResult *out);

/**
 * @brief Discards the FIFO head. Only meaningful after a successful Psd_Peek().
 * @param base psd_core's AXI4-Lite base address.
 */
void Psd_Discard(u32 base);

/** @brief Events currently buffered. @param base psd_core's AXI4-Lite base address. */
u32 Psd_Level(u32 base);
/** @brief Events integrated since the last clear. @param base psd_core's AXI4-Lite base address. */
u32 Psd_EventCount(u32 base);
/** @brief Sticky: a result was dropped because the FIFO was full. @param base psd_core's AXI4-Lite base address. */
int Psd_Overflowed(u32 base);
/** @brief Empties the FIFO and clears overflow and the counter. @param base psd_core's AXI4-Lite base address. */
void Psd_Clear(u32 base);

/**
 * @brief Sets the FIFO watermark that drives irq_o.
 *
 * irq_o asserts once the FIFO level reaches @p level. 0 disables it, which is how a polled
 * bring-up run turns interrupts off without touching the interrupt controller. Draining in
 * batches on a watermark is what keeps the ISR rate off the event rate at high count rates.
 *
 * @param base  psd_core's AXI4-Lite base address.
 * @param level FIFO level at which irq_o asserts; 0 disables the interrupt.
 */
void Psd_SetWatermark(u32 base, u32 level);

/** @brief Self-test. @param base psd_core's AXI4-Lite base address. @return 1 on success, 0 on failure. */
int Psd_SelfTest(u32 base);

#endif /* SRC_PSD_H_ */
