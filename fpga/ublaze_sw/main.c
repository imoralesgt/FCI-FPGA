/*
 * main.c
 *
 * Entry point. Brings the hardware up once (Bringup_Init()), then hands the UART to the ASCII
 * command interface (cli.h) for the rest of the run.
 *
 * The free-running acquisition loop that used to run here unconditionally (Bringup_Run(), still
 * present for that build) printed CSV continuously, which is incompatible with the CLI: its output
 * would interleave with command replies and break any host-side parser. The two are separate
 * builds, not a runtime choice -- main() picks one at compile time by which it calls.
 *
 * The result FIFOs need no periodic servicing here: Cli_Poll() pops them synchronously in response
 * to $RV, and both are sized (32 deep) to absorb the gap between host polls at the design rate --
 * see docs/log/README.md 8a, "Why a result FIFO instead of a DMA channel".
 */

#include "bringup.h"
#include "cli.h"
#include "platform.h"
#include "uart.h"

/**
 * @brief Entry point. Brings the hardware up once, then services the CLI forever.
 * @return Never returns (the CLI poll loop runs until reset); the trailing `return 0` is
 *         unreachable dead code kept only to satisfy int main()'s signature.
 */
int main(void) {
  init_platform();

  /* Before ANY output. Uart_Init() reprograms the 16550's divisor latch, which reconfigures the
   * line -- a character in flight at that moment is corrupted, and everything printed afterwards
   * is at the new rate. Placing it first means the boot self-test is entirely at the operating
   * baud, rather than split across a rate change partway down the log.
   *
   * The host must therefore open the port at UART_BAUD_HZ (uart.h) to read the boot log at all.
   * That is a deliberate trade: one fixed, documented rate beats a rate that changes mid-stream.
   * No-op on an axi_uartlite bitstream, where the baud is fixed at synthesis. */
  Uart_Init();

  Bringup_Init();
  Cli_Init();
  Cli_SetTraceProvider(Bringup_CaptureTrace);

  for (;;)
    Cli_Poll();

  cleanup_platform(); /* unreachable: the loop above runs until reset */
  return 0;
}
