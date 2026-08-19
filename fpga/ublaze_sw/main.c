/*
 * main.c
 *
 * Entry point only. All acquisition bring-up and the continuous capture loop live in bringup.c,
 * behind Bringup_Run(), which does not return -- the acquisition loop runs until reset.
 */

#include "bringup.h"
#include "platform.h"

int main(void) {
  init_platform();

  Bringup_Run(); /* does not return -- see bringup.h */

  cleanup_platform();
  return 0;
}
