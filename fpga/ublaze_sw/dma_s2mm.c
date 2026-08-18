/*
 * dma_s2mm.c
 *
 * See dma_s2mm.h.
 */

#include "dma_s2mm.h"

#include "registers.h"
#include "xil_io.h"

int Dma_ResetCore(u32 dma_baseaddr) {
  Xil_Out32(dma_baseaddr + AXI_DMA_MM2S_DMACR_OFFSET, AXI_DMA_CR_RESET_MASK);

  for (u32 i = 0; i < 1000000; i++) {
    if ((Xil_In32(dma_baseaddr + AXI_DMA_MM2S_DMACR_OFFSET) & AXI_DMA_CR_RESET_MASK) == 0)
      return 1;
  }
  return 0;
}

void DmaS2mm_ArmTransfer(u32 dma_baseaddr, u32 dest_addr, u32 length_bytes) {
  u32 cr;

  Xil_Out32(dma_baseaddr + AXI_DMA_S2MM_DA_OFFSET, dest_addr);

  /* IOC_IrqEn is harmless to leave set even when nobody's listening (axi_intc/MicroBlaze
   * interrupts stay off unless separately enabled via intc.h) -- always setting it here means
   * polled and interrupt-driven callers share the same arm path. */
  cr = Xil_In32(dma_baseaddr + AXI_DMA_S2MM_DMACR_OFFSET);
  Xil_Out32(dma_baseaddr + AXI_DMA_S2MM_DMACR_OFFSET,
            cr | AXI_DMA_CR_RUNSTOP_MASK | AXI_DMA_CR_IOC_IRQ_EN_MASK);

  /* Writing the length register both sets the transfer length and starts the transfer. */
  Xil_Out32(dma_baseaddr + AXI_DMA_S2MM_LENGTH_OFFSET, length_bytes);
}

void DmaMm2s_ArmTransfer(u32 dma_baseaddr, u32 src_addr, u32 length_bytes) {
  u32 cr;

  Xil_Out32(dma_baseaddr + AXI_DMA_MM2S_SA_OFFSET, src_addr);

  cr = Xil_In32(dma_baseaddr + AXI_DMA_MM2S_DMACR_OFFSET);
  Xil_Out32(dma_baseaddr + AXI_DMA_MM2S_DMACR_OFFSET, cr | AXI_DMA_CR_RUNSTOP_MASK);

  Xil_Out32(dma_baseaddr + AXI_DMA_MM2S_LENGTH_OFFSET, length_bytes);
}

DmaS2mmResult DmaS2mm_PollComplete(u32 dma_baseaddr, u32 max_iters) {
  for (u32 i = 0; i < max_iters; i++) {
    u32 sr = Xil_In32(dma_baseaddr + AXI_DMA_S2MM_DMASR_OFFSET);

    if (sr & AXI_DMA_SR_ERR_ALL_MASK)
      return DMA_S2MM_ERROR;
    if (sr & AXI_DMA_SR_IOC_IRQ_MASK)
      return DMA_S2MM_DONE;
  }
  return DMA_S2MM_TIMEOUT;
}

void DmaS2mm_AckComplete(u32 dma_baseaddr) {
  Xil_Out32(dma_baseaddr + AXI_DMA_S2MM_DMASR_OFFSET,
            AXI_DMA_SR_ALL_IRQ_MASK | AXI_DMA_SR_ERR_ALL_MASK);
}
