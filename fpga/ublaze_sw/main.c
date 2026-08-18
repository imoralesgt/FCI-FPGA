/*
 * main.c
 *
 * Entry point only. All acquisition bring-up and the continuous capture loop live in bringup.c,
 * behind Bringup_Run().
 *
 * The Bringup_Run() call is deliberately left commented out: it does not return, and it drives the
 * detector front end (it programs the VGA gain and arms the trigger). Uncomment it to run the
 * acquisition chain.
 */

#include "bringup.h"
#include "platform.h"

int main(void) {
  init_platform();

  /* Bringup_Run(); */ /* does not return -- see bringup.h */

  cleanup_platform();
  return 0;
}
