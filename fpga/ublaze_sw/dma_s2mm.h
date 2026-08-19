/*
 * dma_s2mm.h
 *
 * Direct-register-mode (Simple DMA, no Scatter-Gather) driver, instance-agnostic via a
 * dma_baseaddr parameter -- used for both axi_dma_0 (S2MM carries fci_core's {PSA_l, PSA_w}
 * result beats into axi_bram_ctrl_0; MM2S reads that same BRAM back out to microblaze_0/S0_AXIS)
 * and axi_dma_1 (S2MM carries trigger_core's raw samples via axis_broadcaster_0 into the same
 * BRAM; MM2S reads back out to microblaze_0/S1_AXIS). Either way, axi_bram_ctrl_0 is mapped only
 * into each DMA's own Data_MM2S/Data_S2MM address spaces, never into microblaze_0/Data (see
 * fpga/bd/fci_bd.tcl's assign_bd_address calls), so a plain Xil_In32 on the BRAM address doesn't
 * work -- the CPU must consume each MM2S stream via the FSL get instruction (fsl.h) instead, on
 * whichever stream index that DMA's M_AXIS_MM2S is wired to (0 for axi_dma_0, 1 for axi_dma_1).
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

/* Resets the whole DMA core at dma_baseaddr (both channels -- shared reset per PG021) and waits
 * for the reset to self-clear. Returns 1 on success, 0 on timeout. */
int Dma_ResetCore(u32 dma_baseaddr);

/* Arms an S2MM transfer of length_bytes into dest_addr and starts it. */
void DmaS2mm_ArmTransfer(u32 dma_baseaddr, u32 dest_addr, u32 length_bytes);

/* Arms an MM2S transfer of length_bytes from src_addr and starts it; the data streams out to
 * whichever microblaze_0/SN_AXIS this DMA's M_AXIS_MM2S is wired to. Consume it with blocking
 * getfslx(val, N, FSL_DEFAULT) calls (fsl.h) -- that blocking read is the synchronization point,
 * no status polling needed. Note: if this DMA's stream data width is narrower than its memory
 * data width (e.g. axi_dma_1's 16-bit stream into a 32-bit memory bus), each 32-bit FSL word
 * packs multiple narrow samples -- see the caller for unpacking. */
void DmaMm2s_ArmTransfer(u32 dma_baseaddr, u32 src_addr, u32 length_bytes);

/* Polls S2MM_DMASR up to max_iters times for completion (IOC_Irq) or a DMA error. Does not clear
 * the status bits -- call DmaS2mm_AckComplete() afterward. */
DmaS2mmResult DmaS2mm_PollComplete(u32 dma_baseaddr, u32 max_iters);

/* Write-1-clears the sticky IRQ/error status bits in S2MM_DMASR, needed before the next
 * DmaS2mm_ArmTransfer(). */
void DmaS2mm_AckComplete(u32 dma_baseaddr);

/* Waits (bounded) for an MM2S transfer to complete, so callers can avoid issuing a BLOCKING FSL
 * read for data that will never arrive. Returns 1 on completion, 0 on error or timeout.
 *
 * This exists because getfslx(..., FSL_DEFAULT) compiles to a blocking `get` instruction: it stalls
 * the core until a word appears. service_dma0_event() runs one inside the ISR, so a failed readback
 * wedges the whole firmware with interrupts disabled and no output at all -- observed 2026-08-18,
 * where the serial log stopped mid-sequence at the calibration header while MM2S read HALTED. */
int DmaMm2s_WaitDone(u32 dma_baseaddr, u32 max_iters);

/* Clears a halted-on-error S2MM channel by resetting the core. Returns 1 if an error was present
 * and the reset was performed, 0 if the channel was healthy. The caller must re-arm after a 1. */
int DmaS2mm_RecoverIfHalted(u32 dma_baseaddr);

#endif /* SRC_DMA_S2MM_H_ */
