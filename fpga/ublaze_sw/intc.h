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

/**
 * @brief Enables the given interrupt sources and registers the shared handler. Call once at init.
 *
 * Enables the given bitmask of interrupt sources (e.g. INTC_DMA_S2MM_BIT), asserts axi_intc's
 * Master Enable + Hardware Interrupt Enable, registers @p handler as MicroBlaze's external
 * interrupt vector, and enables interrupts globally (microblaze_enable_interrupts()).
 *
 * @param enable_mask  Bitmask of interrupt sources to enable in axi_intc's IER.
 * @param handler      Shared handler invoked for whichever enabled source(s) are pending.
 * @param callback_ref Opaque pointer passed back to @p handler on each call.
 */
void Intc_Init(u32 enable_mask, XInterruptHandler handler, void *callback_ref);

/**
 * @brief Enables additional interrupt source(s) without disturbing already-enabled ones.
 *
 * Adds bits via axi_intc's Set Interrupt Enable register (sets only the given bits, leaves
 * everything else in IER alone). Use this instead of a second Intc_Init() call -- Intc_Init()
 * writes IER directly, so calling it twice would silently disable whatever the first call enabled.
 *
 * @param additional_mask Bitmask of additional interrupt sources to enable.
 */
void Intc_EnableAdditional(u32 additional_mask);

#endif /* SRC_INTC_H_ */
