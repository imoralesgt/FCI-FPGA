/*
 * psd.h
 *
 * Driver for psd_core (fpga/rtl/psd_core) -- CAEN-style dual-gate charge integration.
 *
 * Register map mirrors psd_axi4lite_regs.vhd; keep the two in sync if that map changes.
 */

#ifndef SRC_PSD_H_
#define SRC_PSD_H_

#include "xil_types.h"

/* One event's worth of PSD output. energy_* are SIGNED: undershoot below the baseline reference
 * genuinely subtracts, and treating them as unsigned would turn a small negative integral into a
 * huge positive one. */
typedef struct {
  s32 energy_short;
  s32 energy_long;
  u64 timestamp; /* trigger_core's 64-bit cycle count at the moment this pulse fired */
} PsdResult;

/* pre_trigger MUST match trigger_core's delay register: it is where the trigger sits inside the
 * captured frame, and psd_core has no other way to know. pre_gate/short_gate/long_gate are the
 * gate geometry in samples at 50 Msps. baseline_ref is the code treated as zero charge -- mid-scale
 * (8192) when fed by blr_core, which re-centers there. */
void Psd_Configure(u32 base, u32 pre_trigger, u32 pre_gate, u32 short_gate, u32 long_gate,
                   u32 baseline_ref);

/* Pops one result if the FIFO is non-empty. Returns 1 on success, 0 if empty. */
int Psd_Pop(u32 base, PsdResult *out);

/* Reads the head without popping -- for pairing against another core's FIFO, where the decision to
 * consume depends on what the other side is holding. */
int Psd_Peek(u32 base, PsdResult *out);

/* Discards the head. Only meaningful after a successful Psd_Peek(). */
void Psd_Discard(u32 base);

u32 Psd_Level(u32 base);        /* events currently buffered */
u32 Psd_EventCount(u32 base);   /* events integrated since the last clear */
int Psd_Overflowed(u32 base);   /* sticky: a result was dropped because the FIFO was full */
void Psd_Clear(u32 base);       /* empties the FIFO and clears overflow and the counter */

/* irq_o asserts once the FIFO level reaches this. 0 disables it, which is how a polled bring-up
 * run turns interrupts off without touching the interrupt controller. Draining in batches on a
 * watermark is what keeps the ISR rate off the event rate at high count rates. */
void Psd_SetWatermark(u32 base, u32 level);

int Psd_SelfTest(u32 base);

#endif /* SRC_PSD_H_ */
