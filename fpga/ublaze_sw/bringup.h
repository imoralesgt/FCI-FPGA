/*
 * bringup.h
 *
 * Entry point for the FCI acquisition bring-up sequence. See bringup.c.
 */

#ifndef SRC_BRINGUP_H_
#define SRC_BRINGUP_H_

#include "xil_types.h"

/**
 * @brief Brings the hardware up and RETURNS.
 *
 * Register checks on trigger_core/fci_core/blr_core/psd_core, VGA gain setup, both
 * interrupt-driven DMA pipelines, automatic threshold calibration, and the end-to-end capture
 * tests -- reporting PASS/FAIL per step over UART. Leaves capture running.
 *
 * The platform must already be initialised (init_platform()) before calling this.
 *
 * Its progress report is plain text, not CLI-framed. A host driving the command interface should
 * discard received lines until the first reply to its own request arrives.
 */
void Bringup_Init(void);

/**
 * @brief Bringup_Init() followed by the free-running CSV acquisition loop. DOES NOT RETURN.
 *
 * Mutually exclusive with the CLI: this loop emits unsolicited lines, which would interleave with
 * command replies.
 */
void Bringup_Run(void);

/**
 * @brief Returns the most recently completed raw trace.
 *
 * Points *out_buf at the most recently completed raw trace (this file's own static storage, not a
 * copy) and writes the sample count to *out_count. Matches CliTraceFn so it can be handed to
 * Cli_SetTraceProvider() -- see that type's comment for why a pointer rather than a copy.
 *
 * @param out_buf     Set to point at the trace's storage on success.
 * @param max_samples Caller's buffer/interest limit on sample count.
 * @param out_count   Set to the number of samples available, on success.
 * @return 1 on success, 0 if no capture has completed yet (out_buf and out_count left untouched).
 */
int Bringup_CaptureTrace(const s16 **out_buf, u32 max_samples, u32 *out_count);

/**
 * @brief Re-arms the raw-trace capture pipeline after the trigger's depth register changes.
 *
 * Must be called immediately after writing a new value to the trigger's depth register (CLI $ST
 * index 3) -- see bringup.c for why a bare register write there can permanently wedge the
 * raw-trace capture pipeline. Resets and re-arms axi_dma_1's S2MM channel to match, bounded (like
 * Dma_ResetCore()), so a hardware fault here fails safely rather than blocking.
 *
 * @param new_depth The trigger depth just written, in samples.
 */
void Bringup_ReconfigureRawTraceDepth(u32 new_depth);

#endif /* SRC_BRINGUP_H_ */
