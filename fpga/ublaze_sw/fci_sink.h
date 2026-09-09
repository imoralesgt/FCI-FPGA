/*
 * fci_sink.h
 *
 * Driver for the buffered AXI4-Lite result window on fci_core's output.
 *
 * The separate fci_sink IP this was named after no longer exists: it was merged into the
 * hand-written fci_core (fpga/rtl/fci_core_rtl), which exposes the same result-window semantics
 * from one register map at one base address. The FciSink_* names are kept because the accessors
 * below work unchanged against the merged map -- see registers.h for what actually moved.
 *
 * Register map mirrors fci_axi4lite_regs.vhd; keep the two in sync if that map changes.
 */

#ifndef SRC_FCI_SINK_H_
#define SRC_FCI_SINK_H_

#include "xil_types.h"

/**
 * @brief One FCI event's window accumulators plus its pairing timestamp.
 *
 * psa_l/psa_w are raw 32-bit unsigned sums of |Re|+|Im| bin magnitudes -- NOT the old HLS core's
 * ap_ufixed<28,12> (Q12.16). The scale is arbitrary but identical for both windows, so the FCI
 * ratio is unaffected; nothing here or downstream should apply a 2^16 divisor any more.
 */
typedef struct {
  u32 psa_l;      /**< FCI numerator window accumulator. */
  u32 psa_w;      /**< FCI denominator window accumulator. */
  u64 timestamp;  /**< trigger_core's 64-bit cycle count at the moment this pulse fired. */
} FciResult;

/**
 * @brief Pops one result if the FIFO is non-empty.
 * @param base fci_core's AXI4-Lite base address.
 * @param out  Filled in on success; untouched otherwise.
 * @return 1 on success, 0 if the FIFO was empty.
 */
int FciSink_Pop(u32 base, FciResult *out);

/**
 * @brief Reads the FIFO head without popping.
 *
 * For pairing against another core's FIFO, where the decision to consume depends on what the
 * other side is holding (see acquisition.c's Acq_PopPaired()).
 *
 * @param base fci_core's AXI4-Lite base address.
 * @param out  Filled in on success; untouched otherwise.
 * @return 1 on success, 0 if the FIFO was empty.
 */
int FciSink_Peek(u32 base, FciResult *out);

/** @brief Discards the FIFO head. Only meaningful after a successful FciSink_Peek(). */
void FciSink_Discard(u32 base);

/** @brief Events currently buffered. @param base fci_core's AXI4-Lite base address. */
u32 FciSink_Level(u32 base);
/** @brief Events integrated since the last clear. @param base fci_core's AXI4-Lite base address. */
u32 FciSink_EventCount(u32 base);
/** @brief Sticky: a result was dropped because the FIFO was full. @param base fci_core's AXI4-Lite base address. */
int FciSink_Overflowed(u32 base);

/**
 * @brief Sticky framing-error flag.
 *
 * Set when a beat arrived where the other kind of beat was expected -- i.e. fci_core did not emit
 * its usual PSA_l/PSA_w pair. Worth reporting rather than ignoring: the pairing re-synchronizes on
 * the next TLAST, so this says "one result was suspect", not "everything after this is wrong".
 *
 * @param base fci_core's AXI4-Lite base address.
 * @return 1 if a framing error has occurred since the last clear, 0 otherwise.
 */
int FciSink_FramingError(u32 base);

/** @brief Empties the FIFO and clears overflow, framing-error, and the event counter. */
void FciSink_Clear(u32 base);

/**
 * @brief Sets the FIFO watermark that drives this core's irq_o.
 * @param base  fci_core's AXI4-Lite base address.
 * @param level FIFO level at which irq_o asserts; 0 disables the interrupt.
 */
void FciSink_SetWatermark(u32 base, u32 level);

/**
 * @brief Computes FCI = PSA_l / PSA_w, scaled by 10000 so it can be printed without floating
 *        point (xil_printf has no \%f).
 * @param r Result to compute the ratio from.
 * @return FCI * 10000, or 0 if psa_w is 0 (the only division hazard here).
 */
u32 FciSink_RatioScaled(const FciResult *r);

#endif /* SRC_FCI_SINK_H_ */
