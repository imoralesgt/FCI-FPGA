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

/* psa_l/psa_w are raw 32-bit unsigned sums of |Re|+|Im| bin magnitudes -- NOT the old HLS core's
 * ap_ufixed<28,12> (Q12.16). The scale is arbitrary but identical for both windows, so the FCI
 * ratio is unaffected; nothing here or downstream should apply a 2^16 divisor any more. */
typedef struct {
  u32 psa_l;
  u32 psa_w;
  u64 timestamp;
} FciResult;

int FciSink_Pop(u32 base, FciResult *out);
int FciSink_Peek(u32 base, FciResult *out);
void FciSink_Discard(u32 base);

u32 FciSink_Level(u32 base);
u32 FciSink_EventCount(u32 base);
int FciSink_Overflowed(u32 base);

/* Sticky. Set when a beat arrived where the other kind of beat was expected -- i.e. fci_core did
 * not emit its usual PSA_l/PSA_w pair. Worth reporting rather than ignoring: the pairing
 * re-synchronizes on the next TLAST, so this says "one result was suspect", not "everything after
 * this is wrong". */
int FciSink_FramingError(u32 base);

void FciSink_Clear(u32 base);
void FciSink_SetWatermark(u32 base, u32 level);

/* FCI = PSA_l / PSA_w, returned scaled by 10000 so it can be printed without floating point
 * (xil_printf has no %f). Returns 0 if psa_w is 0, which is the only division hazard here. */
u32 FciSink_RatioScaled(const FciResult *r);

#endif /* SRC_FCI_SINK_H_ */
