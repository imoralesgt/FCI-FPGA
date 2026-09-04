/*
 * blr.c
 *
 * See blr.h.
 */

#include "blr.h"

#include "registers.h"
#include "xil_io.h"

/** @brief See blr.h. */
void Blr_Configure(u32 base, u32 shift, u32 gate_thr, u32 holdoff) {
  Xil_Out32(base + BLR_SHIFT_OFFSET, shift);
  Xil_Out32(base + BLR_GATE_THR_OFFSET, gate_thr);
  Xil_Out32(base + BLR_HOLDOFF_OFFSET, holdoff);
  /* Leave ctrl alone: bypass/hold are operator controls, not part of a nominal configuration. */
}

/** @brief See blr.h. */
s32 Blr_GetBaseline(u32 base) {
  u32 raw = Xil_In32(base + BLR_STATUS_OFFSET) & BLR_STATUS_BASELINE_MASK;
  /* The register field is BLR_BASELINE_BITS wide and SIGNED. Sign-extend explicitly: the detector
   * baseline genuinely sits below zero on this board, and reading it unsigned would report a
   * plausible-looking large positive number instead of a small negative one. */
  if (raw & (1U << (BLR_BASELINE_BITS - 1)))
    return (s32)(raw | ~((1U << BLR_BASELINE_BITS) - 1U));
  return (s32)raw;
}

/** @brief See blr.h. */
int Blr_GateOpen(u32 base) {
  return (Xil_In32(base + BLR_STATUS_OFFSET) & BLR_STATUS_GATE_OPEN_MASK) ? 1 : 0;
}

/** @brief Read-modify-writes one bit of the ctrl register (set if @p on, clear otherwise). */
static void set_ctrl_bit(u32 base, u32 mask, int on) {
  u32 ctrl = Xil_In32(base + BLR_CTRL_OFFSET);
  if (on)
    ctrl |= mask;
  else
    ctrl &= ~mask;
  Xil_Out32(base + BLR_CTRL_OFFSET, ctrl);
}

/** @brief See blr.h. */
void Blr_SetBypass(u32 base, int on) { set_ctrl_bit(base, BLR_CTRL_BYPASS_MASK, on); }

/** @brief See blr.h. */
void Blr_SetHold(u32 base, int on) { set_ctrl_bit(base, BLR_CTRL_HOLD_MASK, on); }

/** @brief See blr.h. */
int Blr_SelfTest(u32 base) {
  u32 saved_shift = Xil_In32(base + BLR_SHIFT_OFFSET);
  u32 saved_thr = Xil_In32(base + BLR_GATE_THR_OFFSET);
  u32 saved_hold = Xil_In32(base + BLR_HOLDOFF_OFFSET);
  int ok = 1;

  /* Patterns chosen inside each field's real width (shift is 4 bits, gate_thr 14, holdoff 12), so
   * a failure means the register is broken rather than that the test overflowed it. */
  Xil_Out32(base + BLR_SHIFT_OFFSET, 9);
  Xil_Out32(base + BLR_GATE_THR_OFFSET, 0x1234);
  Xil_Out32(base + BLR_HOLDOFF_OFFSET, 0x555);

  if ((Xil_In32(base + BLR_SHIFT_OFFSET) & 0xF) != 9)
    ok = 0;
  if ((Xil_In32(base + BLR_GATE_THR_OFFSET) & 0x3FFF) != 0x1234)
    ok = 0;
  if ((Xil_In32(base + BLR_HOLDOFF_OFFSET) & 0xFFF) != 0x555)
    ok = 0;

  Xil_Out32(base + BLR_SHIFT_OFFSET, saved_shift);
  Xil_Out32(base + BLR_GATE_THR_OFFSET, saved_thr);
  Xil_Out32(base + BLR_HOLDOFF_OFFSET, saved_hold);
  return ok;
}

/** @brief See blr.h. */
u32 Blr_GateThresholdForSigma(u32 sigma) {
  u32 thr = sigma * 4u;
  if (thr < 32u)
    thr = 32u; /* a dead or unusually quiet input must not weld the gate shut */
  if (thr > 1024u)
    thr = 1024u; /* beyond this the gate stops rejecting pulses on this detector */
  return thr;
}
