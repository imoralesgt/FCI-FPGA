/*
 * intc.h
 *
 * Minimal AXI INTC driver: standard (non-fast, non-vectored) interrupt mode -- axi_intc's
 * C_HAS_FAST capability is left unused for now, matching the project's default of building the
 * simplest correct thing first. A single shared handler services whichever enabled source(s) are
 * pending; MicroBlaze only has one external interrupt line in (fed by axi_intc's demuxed output),
 * so there's exactly one vector to register regardless of how many interrupt sources exist.
 */

#ifndef SRC_INTC_H_
#define SRC_INTC_H_

#include "xil_exception.h" /* XInterruptHandler */
#include "xil_types.h"

/* Enables the given bitmask of interrupt sources (e.g. INTC_DMA_S2MM_BIT), asserts axi_intc's
 * Master Enable + Hardware Interrupt Enable, registers handler as MicroBlaze's external interrupt
 * vector, and enables interrupts globally (microblaze_enable_interrupts()). Call once at init. */
void Intc_Init(u32 enable_mask, XInterruptHandler handler, void *callback_ref);

#endif /* SRC_INTC_H_ */
