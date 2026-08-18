/*
 * intc.c
 *
 * See intc.h.
 */

#include "intc.h"

#include "mb_interface.h"
#include "registers.h"
#include "xil_io.h"

void Intc_Init(u32 enable_mask, XInterruptHandler handler, void *callback_ref) {
  Xil_Out32(AXI_INTC_BASEADDR + AXI_INTC_IER_OFFSET, enable_mask);
  Xil_Out32(AXI_INTC_BASEADDR + AXI_INTC_MER_OFFSET,
            AXI_INTC_MER_ME_MASK | AXI_INTC_MER_HIE_MASK);

  microblaze_register_handler(handler, callback_ref);
  microblaze_enable_interrupts();
}

void Intc_EnableAdditional(u32 additional_mask) {
  Xil_Out32(AXI_INTC_BASEADDR + AXI_INTC_SIE_OFFSET, additional_mask);
}
