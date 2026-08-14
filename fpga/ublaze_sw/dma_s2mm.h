/*
 * dma_s2mm.h
 *
 * Direct-register-mode (Simple DMA, no Scatter-Gather) driver for axi_dma_0: S2MM carries
 * fci_core's {PSA_l, PSA_w} result beats into axi_bram_ctrl_0; MM2S reads that same BRAM back out
 * to microblaze_0/S0_AXIS, the *only* CPU-reachable path to it -- axi_bram_ctrl_0 is mapped into
 * axi_dma_0's own Data_MM2S/Data_S2MM address spaces only, not into microblaze_0/Data (see
 * fpga/bd/fci_bd.tcl's assign_bd_address calls), so a plain Xil_In32 on the BRAM address doesn't
 * work -- the CPU must consume the MM2S stream via the FSL get instruction (fsl.h) instead.
 *
 * Register sequencing (reset, arm-transfer field order) matches the Vitis-generated XAxiDma
 * driver's own XAxiDma_Reset()/XAxiDma_SimpleTransfer(), reimplemented directly against the
 * registers rather than pulling in the full BD-ring driver, since this project accesses every
 * other custom/simple peripheral (trigger_core, fci_core, the VGA DAC) the same way.
 */

#ifndef SRC_DMA_S2MM_H_
#define SRC_DMA_S2MM_H_

#include "xil_types.h"

typedef enum { DMA_S2MM_DONE, DMA_S2MM_TIMEOUT, DMA_S2MM_ERROR } DmaS2mmResult;

/* Resets the whole DMA core (both channels -- shared reset per PG021) and waits for the reset to
 * self-clear. Returns 1 on success, 0 on timeout. */
int Dma_ResetCore(void);

/* Arms an S2MM transfer of length_bytes into dest_addr and starts it. */
void DmaS2mm_ArmTransfer(u32 dest_addr, u32 length_bytes);

/* Arms an MM2S transfer of length_bytes from src_addr and starts it; the data streams out to
 * microblaze_0/S0_AXIS. Consume it with length_bytes/4 blocking getfslx(val, 0, FSL_DEFAULT)
 * calls (fsl.h) -- that blocking read is the synchronization point, no status polling needed. */
void DmaMm2s_ArmTransfer(u32 src_addr, u32 length_bytes);

/* Polls S2MM_DMASR up to max_iters times for completion (IOC_Irq) or a DMA error. Does not clear
 * the status bits -- call DmaS2mm_AckComplete() afterward. */
DmaS2mmResult DmaS2mm_PollComplete(u32 max_iters);

/* Write-1-clears the sticky IRQ/error status bits in S2MM_DMASR, needed before the next
 * DmaS2mm_ArmTransfer(). */
void DmaS2mm_AckComplete(void);

#endif /* SRC_DMA_S2MM_H_ */
