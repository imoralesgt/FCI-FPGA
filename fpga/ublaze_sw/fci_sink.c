/*
 * fci_sink.c
 *
 * See fci_sink.h.
 */

#include "fci_sink.h"

#include "registers.h"
#include "xil_io.h"

int FciSink_Peek(u32 base, FciResult *out) {
  u32 status = Xil_In32(base + FCI_SINK_STATUS_OFFSET);
  if (status & FCI_SINK_STATUS_EMPTY_MASK)
    return 0;

  /* fci_core's results are 28-bit in a 32-bit word; mask so the unused top bits can never leak
   * into the ratio. */
  out->psa_l = Xil_In32(base + FCI_SINK_PSA_L_OFFSET) & 0x0FFFFFFFu;
  out->psa_w = Xil_In32(base + FCI_SINK_PSA_W_OFFSET) & 0x0FFFFFFFu;
  out->timestamp = ((u64)Xil_In32(base + FCI_SINK_TS_HI_OFFSET) << 32) |
                   (u64)Xil_In32(base + FCI_SINK_TS_LO_OFFSET);
  return 1;
}

void FciSink_Discard(u32 base) { Xil_Out32(base + FCI_SINK_CTRL_OFFSET, FCI_SINK_CTRL_POP_MASK); }

int FciSink_Pop(u32 base, FciResult *out) {
  if (!FciSink_Peek(base, out))
    return 0;
  FciSink_Discard(base);
  return 1;
}

u32 FciSink_Level(u32 base) {
  return (Xil_In32(base + FCI_SINK_STATUS_OFFSET) >> FCI_SINK_STATUS_LEVEL_SHIFT) &
         FCI_SINK_STATUS_LEVEL_MASK;
}

u32 FciSink_EventCount(u32 base) { return Xil_In32(base + FCI_SINK_EVENT_COUNT_OFFSET); }

int FciSink_Overflowed(u32 base) {
  return (Xil_In32(base + FCI_SINK_STATUS_OFFSET) & FCI_SINK_STATUS_OVERFLOW_MASK) ? 1 : 0;
}

int FciSink_FramingError(u32 base) {
  return (Xil_In32(base + FCI_SINK_STATUS_OFFSET) & FCI_SINK_STATUS_FRAMING_ERR_MASK) ? 1 : 0;
}

void FciSink_Clear(u32 base) { Xil_Out32(base + FCI_SINK_CTRL_OFFSET, FCI_SINK_CTRL_CLEAR_MASK); }

void FciSink_SetWatermark(u32 base, u32 level) {
  Xil_Out32(base + FCI_SINK_WATERMARK_OFFSET, level);
}

u32 FciSink_RatioScaled(const FciResult *r) {
  if (r->psa_w == 0u)
    return 0u;
  /* Both operands share the same Q12.16 scale, so it cancels and the raw codes divide directly.
   * 64-bit intermediate because psa_l * 10000 overflows 32 bits for any psa_l above ~429k, and
   * psa_l runs to 2^28. */
  return (u32)(((u64)r->psa_l * 10000ULL) / (u64)r->psa_w);
}
