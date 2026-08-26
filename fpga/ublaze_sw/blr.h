/*
 * blr.h
 *
 * Driver for blr_core (fpga/rtl/blr_core) -- the continuous baseline restorer that sits between
 * the ADC pins and trigger_core.
 *
 * Register map mirrors blr_axi4lite_regs.vhd; keep the two in sync if that map changes.
 */

#ifndef SRC_BLR_H_
#define SRC_BLR_H_

#include "xil_types.h"

/* Reset defaults, repeated here so firmware can report "as configured" vs "as left by reset"
 * without reading them back. Match blr_axi4lite_regs.vhd's reset values. */
#define BLR_DEFAULT_SHIFT 12
#define BLR_DEFAULT_GATE_THR 256
#define BLR_DEFAULT_HOLDOFF 384

/* The estimator's time constant is exactly 2^shift samples (see baseline_estimator.vhd), so at
 * 50 Msps: shift 10 = 20.5 us, 12 = 82 us, 14 = 328 us. It must be SLOW against the pulse decay
 * (tau ~ 1.4 us on this detector) or the estimator tracks the pulse and subtracts the signal away,
 * and FAST against real DC drift. 12 is two orders of magnitude clear of the pulse, which is the
 * margin this constant is chosen for. */
void Blr_Configure(u32 base, u32 shift, u32 gate_thr, u32 holdoff);

/* Live estimate, SIGNED ADC code. Reads the estimator directly rather than a stored register, so
 * this is what the hardware is using right now. Negative is normal: this detector's baseline sits
 * below zero. */
s32 Blr_GetBaseline(u32 base);

/* 1 while the estimator is tracking, 0 while the gate is shut (during a pulse or its hold-off).
 * Sampled asynchronously, so a single read is a snapshot, not a duty cycle. */
int Blr_GateOpen(u32 base);

/* Bypass forwards the converted sample untouched, at the same latency as the restored path, so
 * toggling it at runtime does not shift the stream in time. That equal latency is what makes an
 * A/B comparison of restored vs unrestored data meaningful. */
void Blr_SetBypass(u32 base, int on);

/* Freezes the estimate without stopping the datapath -- useful to hold a known baseline while
 * sweeping something else. */
void Blr_SetHold(u32 base, int on);

/* Writes a known pattern to each writable register and reads it back. Returns 1 on success.
 * Restores the caller's configuration afterwards. */
int Blr_SelfTest(u32 base);

/* Derives the gate threshold from a measured baseline sigma. The gate must stay OPEN on noise and
 * SHUT on pulses, so the threshold belongs a few sigma above the noise: below that it chatters and
 * the estimator stops tracking; far above it, small pulses leak into the average and drag it. This
 * project measures sigma ~7 counts quiet and ~55 at 30 cps, so the useful range is roughly 30..250.
 * Clamped to a sane band because a sigma of 0 (a dead input) would otherwise weld the gate shut. */
u32 Blr_GateThresholdForSigma(u32 sigma);

#endif /* SRC_BLR_H_ */
