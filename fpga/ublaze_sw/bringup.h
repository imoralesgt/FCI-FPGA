/*
 * bringup.h
 *
 * Entry point for the FCI acquisition bring-up sequence. See bringup.c.
 */

#ifndef SRC_BRINGUP_H_
#define SRC_BRINGUP_H_

/* Runs the full bring-up: register checks on trigger_core/fci_core, VGA gain setup, both
 * interrupt-driven DMA pipelines, automatic threshold calibration, and the end-to-end capture
 * tests -- reporting PASS/FAIL per step over UART. Then hands off to continuous interrupt-driven
 * acquisition, printing one FCI value per event.
 *
 * DOES NOT RETURN: the acquisition loop runs until reset. The platform must already be initialized
 * (init_platform()) before calling this. */
void Bringup_Run(void);

#endif /* SRC_BRINGUP_H_ */
