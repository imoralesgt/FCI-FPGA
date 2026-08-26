/*
 * bringup.h
 *
 * Entry point for the FCI acquisition bring-up sequence. See bringup.c.
 */

#ifndef SRC_BRINGUP_H_
#define SRC_BRINGUP_H_

#include "xil_types.h"

/* Brings the hardware up and RETURNS: register checks on trigger_core/fci_core/blr_core/psd_core,
 * VGA gain setup, both interrupt-driven DMA pipelines, automatic threshold calibration, and the
 * end-to-end capture tests -- reporting PASS/FAIL per step over UART. Leaves capture running.
 *
 * The platform must already be initialised (init_platform()) before calling this.
 *
 * Its progress report is plain text, not CLI-framed. A host driving the command interface should
 * discard received lines until the first reply to its own request arrives. */
void Bringup_Init(void);

/* Bringup_Init() followed by the free-running CSV acquisition loop. DOES NOT RETURN.
 *
 * Mutually exclusive with the CLI: this loop emits unsolicited lines, which would interleave with
 * command replies. */
void Bringup_Run(void);

/* Copies the most recently completed raw trace into buf, writing the sample count to out_count.
 * Returns 1 on success, 0 if no capture has completed yet. Matches CliTraceFn so it can be handed
 * to Cli_SetTraceProvider(). */
int Bringup_CaptureTrace(s16 *buf, u32 max_samples, u32 *out_count);

#endif /* SRC_BRINGUP_H_ */
