/*
 * cli.h
 *
 * ASCII command interface over the AXI UARTLITE -- repo issue #15.
 *
 * Framing follows the NSIL-Counter CLI specification so that one host-side parser can drive both
 * instruments:
 *
 *   request  $CC [v1 v2 ...]\n     '$' + two-character code + space-separated 32-bit values
 *   reply    !CC [v1 v2 ...]\n     '!' + the same code, echoed
 *   error    !XX <code>\n          0 = unknown command, 1 = bad or out-of-range parameter
 *
 * Set commands acknowledge with the bare code (`!SB\n`); get commands echo the selector before the
 * value (`$GB 1\n` -> `!GB 1 256\n`), which is what makes a reply self-describing when several are
 * in flight in a terminal log.
 *
 * The interface is strictly request/response: nothing is ever emitted unsolicited. That is a
 * deliberate constraint -- the acquisition loop this replaces printed CSV continuously, which would
 * interleave with replies and break any host-side parser. Events are now pulled with $RV.
 *
 * See docs/CLI_documentation.md for the full command reference.
 */

#ifndef SRC_CLI_H_
#define SRC_CLI_H_

#include "xil_types.h"

/* Longest raw trace $RT will return. Matches bringup.c's RAW_TRACE_MAX_SAMPLES. */
#define CLI_TRACE_MAX 2048

/* Fills buf with up to max_samples of the most recent captured trace, writing the sample count to
 * out_count. Returns 1 on success, 0 if no trace could be captured.
 *
 * Registered rather than called directly because the capture path owns the DMA and the raw-trace
 * BRAM, and cli.c has no business knowing about either. */
typedef int (*CliTraceFn)(s16 *buf, u32 max_samples, u32 *out_count);
void Cli_SetTraceProvider(CliTraceFn fn);

/* Call once after the cores are configured. */
void Cli_Init(void);

/* Non-blocking. Drains whatever the UART has received, and executes a command once a full line has
 * arrived. Call from the main loop as often as convenient; one call handles at most one command so
 * a flood of input cannot starve the rest of the loop. */
void Cli_Poll(void);

/* True while acquisition is enabled ($AE / $AD). $RV consults this internally; exposed for a
 * caller that wants to reflect the state elsewhere (a status LED, a log line) without adding a
 * second $ES round trip. */
int Cli_AcquisitionEnabled(void);

#endif /* SRC_CLI_H_ */
