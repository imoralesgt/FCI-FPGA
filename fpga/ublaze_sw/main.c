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

int main(void) {
  init_platform();

  Bringup_Init();
  Cli_Init();
  Cli_SetTraceProvider(Bringup_CaptureTrace);

  for (;;)
    Cli_Poll();

  cleanup_platform(); /* unreachable: the loop above runs until reset */
  return 0;
}
