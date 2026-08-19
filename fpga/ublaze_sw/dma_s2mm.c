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

/* Waits (bounded) for a channel to leave the halted state after RS is set. Returns 1 if running.
 *
 * PG021's programming sequence is: set DMACR.RS, WAIT for DMASR.Halted to clear, write the
 * address, then write the length (which starts the transfer). Skipping the wait is harmless only
 * while the channel is already running -- which was true until error recovery started resetting
 * the core, because a reset drives RS back to 0. A length written to a still-halted channel does
 * not take: the transfer completes immediately having moved 0 bytes and raises DMAIntErr (the
 * len=0 case), which recovery then resets and retries, spinning at ~75k errors/second with the
 * stream never touched at all. */
static int wait_running(u32 sr_addr) {
  for (u32 i = 0; i < 100000; i++) {
    if ((Xil_In32(sr_addr) & XAXIDMA_HALTED_MASK) == 0)
      return 1;
  }
  return 0;
}

void DmaS2mm_ArmTransfer(u32 dma_baseaddr, u32 dest_addr, u32 length_bytes) {
  u32 cr;

  /* IOC_IrqEn is harmless to leave set even when nobody's listening (axi_intc/MicroBlaze
   * interrupts stay off unless separately enabled via intc.h) -- always setting it here means
   * polled and interrupt-driven callers share the same arm path. */
  cr = Xil_In32(dma_baseaddr + AXI_DMA_S2MM_DMACR_OFFSET);
  Xil_Out32(dma_baseaddr + AXI_DMA_S2MM_DMACR_OFFSET,
            cr | AXI_DMA_CR_RUNSTOP_MASK | AXI_DMA_CR_IOC_IRQ_EN_MASK);

  (void)wait_running(dma_baseaddr + AXI_DMA_S2MM_DMASR_OFFSET);

  Xil_Out32(dma_baseaddr + AXI_DMA_S2MM_DA_OFFSET, dest_addr);

  /* Writing the length register both sets the transfer length and starts the transfer. */
  Xil_Out32(dma_baseaddr + AXI_DMA_S2MM_LENGTH_OFFSET, length_bytes);
}

void DmaMm2s_ArmTransfer(u32 dma_baseaddr, u32 src_addr, u32 length_bytes) {
  u32 cr;

  /* IOC_IrqEn is set here for the same reason the S2MM arm sets it: it makes DMASR.IOC_Irq a
   * usable completion flag. Without it there is no way to tell whether the transfer delivered, and
   * the only alternative is a blocking FSL read that hangs the CPU when it did not. The MicroBlaze
   * interrupt for this line is not enabled in axi_intc, so setting it raises no interrupt. */
  cr = Xil_In32(dma_baseaddr + AXI_DMA_MM2S_DMACR_OFFSET);
  Xil_Out32(dma_baseaddr + AXI_DMA_MM2S_DMACR_OFFSET,
            cr | AXI_DMA_CR_RUNSTOP_MASK | AXI_DMA_CR_IOC_IRQ_EN_MASK);

  (void)wait_running(dma_baseaddr + AXI_DMA_MM2S_DMASR_OFFSET); /* same requirement as S2MM */

  Xil_Out32(dma_baseaddr + AXI_DMA_MM2S_SA_OFFSET, src_addr);
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

int DmaMm2s_WaitDone(u32 dma_baseaddr, u32 max_iters) {
  for (u32 i = 0; i < max_iters; i++) {
    u32 sr = Xil_In32(dma_baseaddr + AXI_DMA_MM2S_DMASR_OFFSET);
    if (sr & (XAXIDMA_ERR_INTERNAL_MASK | XAXIDMA_ERR_SLAVE_MASK | XAXIDMA_ERR_DECODE_MASK))
      return 0; /* errored: the words are never coming */
    if (sr & AXI_DMA_SR_IOC_IRQ_MASK)
      return 1;
  }
  return 0;
}

int DmaS2mm_RecoverIfHalted(u32 dma_baseaddr) {
  u32 sr = Xil_In32(dma_baseaddr + AXI_DMA_S2MM_DMASR_OFFSET);
  if ((sr & AXI_DMA_SR_ERR_ALL_MASK) == 0)
    return 0; /* nothing to do */

  /* PG021: DMAIntErr/DMASlvErr/DMADecErr are NOT write-1-to-clear. The channel halts and stays
   * halted until DMACR.RS is toggled or the core is reset -- DmaS2mm_AckComplete() writing those
   * bits does nothing for them. Without this, a single error is permanently fatal: the arm path
   * ORs RS in without toggling it, so every later transfer is ignored, and because
   * axis_broadcaster_0 is lockstep the stalled channel takes fci_core and trigger_core down too.
   * A full core reset is the reliable clear; the caller re-arms afterwards. */
  Dma_ResetCore(dma_baseaddr);
  return 1;
}
