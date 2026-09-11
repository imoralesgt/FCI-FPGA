/*
 * pulse_shaper.c
 *
 * See pulse_shaper.h.
 */

#include "pulse_shaper.h"

#include "registers.h"
#include "xil_io.h"

/** @brief See pulse_shaper.h. */
s32 PulseShaper_DecayRecip(u32 decay) {
  return (s32)((1u << PULSE_SHAPER_DECAY_RECIP_FRAC_BITS) / decay);
}

/** @brief See pulse_shaper.h. */
void PulseShaper_Configure(u32 base, u32 peaking, u32 flat_top, u32 decay, u32 enable) {
  Xil_Out32(base + PULSE_SHAPER_PEAKING_OFFSET, peaking);
  Xil_Out32(base + PULSE_SHAPER_FLAT_TOP_OFFSET, flat_top);
  Xil_Out32(base + PULSE_SHAPER_DECAY_OFFSET, decay);
  Xil_Out32(base + PULSE_SHAPER_DECAY_RECIP_OFFSET, (u32)PulseShaper_DecayRecip(decay));
  Xil_Out32(base + PULSE_SHAPER_ENABLE_OFFSET, enable);
}

/** @brief See pulse_shaper.h. */
int PulseShaper_Peek(u32 base, PulseShaperResult *out) {
  u32 status = Xil_In32(base + PULSE_SHAPER_STATUS_OFFSET);
  if (status & PULSE_SHAPER_STATUS_EMPTY_MASK)
    return 0;

  out->amplitude = (s32)Xil_In32(base + PULSE_SHAPER_AMPLITUDE_OFFSET);
  out->timestamp = ((u64)Xil_In32(base + PULSE_SHAPER_TS_HI_OFFSET) << 32) |
                   (u64)Xil_In32(base + PULSE_SHAPER_TS_LO_OFFSET);
  return 1;
}

/** @brief See pulse_shaper.h. */
void PulseShaper_Discard(u32 base) { Xil_Out32(base + PULSE_SHAPER_CTRL_OFFSET, PULSE_SHAPER_CTRL_POP_MASK); }

/** @brief See pulse_shaper.h. */
int PulseShaper_Pop(u32 base, PulseShaperResult *out) {
  if (!PulseShaper_Peek(base, out))
    return 0;
  PulseShaper_Discard(base);
  return 1;
}

/** @brief See pulse_shaper.h. */
u32 PulseShaper_Level(u32 base) {
  return (Xil_In32(base + PULSE_SHAPER_STATUS_OFFSET) >> PULSE_SHAPER_STATUS_LEVEL_SHIFT) &
         PULSE_SHAPER_STATUS_LEVEL_MASK;
}

/** @brief See pulse_shaper.h. */
u32 PulseShaper_EventCount(u32 base) { return Xil_In32(base + PULSE_SHAPER_EVENT_COUNT_OFFSET); }

/** @brief See pulse_shaper.h. */
int PulseShaper_Overflowed(u32 base) {
  return (Xil_In32(base + PULSE_SHAPER_STATUS_OFFSET) & PULSE_SHAPER_STATUS_OVERFLOW_MASK) ? 1 : 0;
}

/** @brief See pulse_shaper.h. */
void PulseShaper_Clear(u32 base) { Xil_Out32(base + PULSE_SHAPER_CTRL_OFFSET, PULSE_SHAPER_CTRL_CLEAR_MASK); }

/** @brief See pulse_shaper.h. */
void PulseShaper_SetWatermark(u32 base, u32 level) {
  Xil_Out32(base + PULSE_SHAPER_WATERMARK_OFFSET, level);
}

/** @brief See pulse_shaper.h. */
int PulseShaper_SelfTest(u32 base) {
  u32 saved_peaking = Xil_In32(base + PULSE_SHAPER_PEAKING_OFFSET);
  u32 saved_flat_top = Xil_In32(base + PULSE_SHAPER_FLAT_TOP_OFFSET);
  u32 saved_decay = Xil_In32(base + PULSE_SHAPER_DECAY_OFFSET);
  int ok = 1;

  /* Values inside each field's own spec range (10..256 / 0..256 / 2..300), so the register-file's
   * own saturating clamp cannot mask a genuine read/write fault here. */
  Xil_Out32(base + PULSE_SHAPER_PEAKING_OFFSET, 123);
  Xil_Out32(base + PULSE_SHAPER_FLAT_TOP_OFFSET, 45);
  Xil_Out32(base + PULSE_SHAPER_DECAY_OFFSET, 210);

  if (Xil_In32(base + PULSE_SHAPER_PEAKING_OFFSET) != 123)
    ok = 0;
  if (Xil_In32(base + PULSE_SHAPER_FLAT_TOP_OFFSET) != 45)
    ok = 0;
  if (Xil_In32(base + PULSE_SHAPER_DECAY_OFFSET) != 210)
    ok = 0;

  Xil_Out32(base + PULSE_SHAPER_PEAKING_OFFSET, saved_peaking);
  Xil_Out32(base + PULSE_SHAPER_FLAT_TOP_OFFSET, saved_flat_top);
  Xil_Out32(base + PULSE_SHAPER_DECAY_OFFSET, saved_decay);
  return ok;
}
