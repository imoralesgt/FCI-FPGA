/*
 * psd.c
 *
 * See psd.h.
 */

#include "psd.h"

#include "registers.h"
#include "xil_io.h"

void Psd_Configure(u32 base, u32 pre_trigger, u32 pre_gate, u32 short_gate, u32 long_gate,
                   u32 baseline_ref) {
  Xil_Out32(base + PSD_PRE_TRIGGER_OFFSET, pre_trigger);
  Xil_Out32(base + PSD_PRE_GATE_OFFSET, pre_gate);
  Xil_Out32(base + PSD_SHORT_GATE_OFFSET, short_gate);
  Xil_Out32(base + PSD_LONG_GATE_OFFSET, long_gate);
  Xil_Out32(base + PSD_BASELINE_REF_OFFSET, baseline_ref);
}

int Psd_Peek(u32 base, PsdResult *out) {
  u32 status = Xil_In32(base + PSD_STATUS_OFFSET);
  if (status & PSD_STATUS_EMPTY_MASK)
    return 0;

  out->energy_short = (s32)Xil_In32(base + PSD_ENERGY_SHORT_OFFSET);
  out->energy_long = (s32)Xil_In32(base + PSD_ENERGY_LONG_OFFSET);
  out->timestamp = ((u64)Xil_In32(base + PSD_TS_HI_OFFSET) << 32) |
                   (u64)Xil_In32(base + PSD_TS_LO_OFFSET);
  return 1;
}

void Psd_Discard(u32 base) { Xil_Out32(base + PSD_CTRL_OFFSET, PSD_CTRL_POP_MASK); }

int Psd_Pop(u32 base, PsdResult *out) {
  if (!Psd_Peek(base, out))
    return 0;
  Psd_Discard(base);
  return 1;
}

u32 Psd_Level(u32 base) {
  return (Xil_In32(base + PSD_STATUS_OFFSET) >> PSD_STATUS_LEVEL_SHIFT) & PSD_STATUS_LEVEL_MASK;
}

u32 Psd_EventCount(u32 base) { return Xil_In32(base + PSD_EVENT_COUNT_OFFSET); }

int Psd_Overflowed(u32 base) {
  return (Xil_In32(base + PSD_STATUS_OFFSET) & PSD_STATUS_OVERFLOW_MASK) ? 1 : 0;
}

void Psd_Clear(u32 base) { Xil_Out32(base + PSD_CTRL_OFFSET, PSD_CTRL_CLEAR_MASK); }

void Psd_SetWatermark(u32 base, u32 level) { Xil_Out32(base + PSD_WATERMARK_OFFSET, level); }

int Psd_SelfTest(u32 base) {
  u32 saved_pt = Xil_In32(base + PSD_PRE_TRIGGER_OFFSET);
  u32 saved_pg = Xil_In32(base + PSD_PRE_GATE_OFFSET);
  u32 saved_sg = Xil_In32(base + PSD_SHORT_GATE_OFFSET);
  u32 saved_lg = Xil_In32(base + PSD_LONG_GATE_OFFSET);
  int ok = 1;

  /* All four are 12-bit fields (clog2(MAX_DEPTH) with MAX_DEPTH=4096), so these patterns fit. */
  Xil_Out32(base + PSD_PRE_TRIGGER_OFFSET, 0x123);
  Xil_Out32(base + PSD_PRE_GATE_OFFSET, 0x045);
  Xil_Out32(base + PSD_SHORT_GATE_OFFSET, 0x678);
  Xil_Out32(base + PSD_LONG_GATE_OFFSET, 0x9AB);

  if ((Xil_In32(base + PSD_PRE_TRIGGER_OFFSET) & 0xFFF) != 0x123)
    ok = 0;
  if ((Xil_In32(base + PSD_PRE_GATE_OFFSET) & 0xFFF) != 0x045)
    ok = 0;
  if ((Xil_In32(base + PSD_SHORT_GATE_OFFSET) & 0xFFF) != 0x678)
    ok = 0;
  if ((Xil_In32(base + PSD_LONG_GATE_OFFSET) & 0xFFF) != 0x9AB)
    ok = 0;

  Xil_Out32(base + PSD_PRE_TRIGGER_OFFSET, saved_pt);
  Xil_Out32(base + PSD_PRE_GATE_OFFSET, saved_pg);
  Xil_Out32(base + PSD_SHORT_GATE_OFFSET, saved_sg);
  Xil_Out32(base + PSD_LONG_GATE_OFFSET, saved_lg);
  return ok;
}
