/*
 * main.c
 *
 * Register-level bring-up test for the FCI acquisition chain's custom IP: writes known values to
 * trigger_core's and fci_core's AXI4-Lite registers and reads them back over UART, without
 * touching DMA or interrupts yet. Confirms the block design's address map and both custom cores
 * are alive before building the DMA/interrupt-driven pipeline on top.
 *
 * fci_core is only poked (window-bound registers + auto_restart), never started (ap_start): with
 * real ap_ctrl_hs dataflow, starting it here would stall waiting on AXI-Stream data from
 * trigger_core that never arrives without a live triggered ADC capture.
 */

#include "platform.h"
#include "registers.h"
#include "xil_io.h"
#include "xil_printf.h"

static int g_fail_count = 0;

static void check_u32(const char *name, u32 expected, u32 actual) {
  if (expected == actual) {
    xil_printf("  [PASS] %s = 0x%08x\r\n", name, actual);
  } else {
    xil_printf("  [FAIL] %s: wrote 0x%08x, read back 0x%08x\r\n", name, expected, actual);
    g_fail_count++;
  }
}

static void test_trigger_core(void) {
  xil_printf("-- trigger_core @ 0x%08x --\r\n", TRIGGER_CORE_BASEADDR);

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, 0x1234);
  check_u32("threshold", 0x1234, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET));

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
  check_u32("polarity", TRIGGER_CORE_POLARITY_RISING,
            Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET));

  /* Mid-range values: no hardware clamping expected (valid range 2..256 / 1..4096). */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, 100);
  check_u32("delay", 100, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET));

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  check_u32("depth", 1024, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET));

  /* Restore the real operating point before leaving: this detector's pulses dip below baseline,
   * so production use is polarity=falling with threshold set below it, not this test's values. */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_FALLING);
}

static void test_fci_core(void) {
  xil_printf("-- fci_core @ 0x%08x --\r\n", FCI_CORE_BASEADDR);

  u32 idle = Xil_In32(FCI_CORE_BASEADDR + FCI_CORE_AP_CTRL_OFFSET) & FCI_CORE_AP_CTRL_IDLE;
  xil_printf("  [INFO] ap_idle = %d (expect 1: core not yet started)\r\n", idle ? 1 : 0);

  /* PSA window bounds matching the values already verified in fci_core_tb.cpp. */
  Xil_Out32(FCI_CORE_BASEADDR + FCI_CORE_PSA_L_LO_OFFSET, 1);
  check_u32("psa_l_lo", 1, Xil_In32(FCI_CORE_BASEADDR + FCI_CORE_PSA_L_LO_OFFSET));

  Xil_Out32(FCI_CORE_BASEADDR + FCI_CORE_PSA_L_HI_OFFSET, 25);
  check_u32("psa_l_hi", 25, Xil_In32(FCI_CORE_BASEADDR + FCI_CORE_PSA_L_HI_OFFSET));

  Xil_Out32(FCI_CORE_BASEADDR + FCI_CORE_PSA_W_LO_OFFSET, 1);
  check_u32("psa_w_lo", 1, Xil_In32(FCI_CORE_BASEADDR + FCI_CORE_PSA_W_LO_OFFSET));

  Xil_Out32(FCI_CORE_BASEADDR + FCI_CORE_PSA_W_HI_OFFSET, 90);
  check_u32("psa_w_hi", 90, Xil_In32(FCI_CORE_BASEADDR + FCI_CORE_PSA_W_HI_OFFSET));

  /* auto_restart only, not ap_start -- starting here would stall the dataflow region waiting on
   * trigger_core's AXI-Stream, which stays silent without a live ADC trigger. */
  Xil_Out32(FCI_CORE_BASEADDR + FCI_CORE_AP_CTRL_OFFSET, FCI_CORE_AP_CTRL_AUTO_RESTART);
  check_u32("ap_ctrl (auto_restart)", FCI_CORE_AP_CTRL_AUTO_RESTART,
            Xil_In32(FCI_CORE_BASEADDR + FCI_CORE_AP_CTRL_OFFSET) & FCI_CORE_AP_CTRL_AUTO_RESTART);
}

int main() {
  init_platform();

  xil_printf("\r\n=== FCI register bring-up test ===\r\n");
  test_trigger_core();
  test_fci_core();

  xil_printf("=== %s (%d failure%s) ===\r\n", g_fail_count == 0 ? "TEST PASSED" : "TEST FAILED",
             g_fail_count, g_fail_count == 1 ? "" : "s");

  cleanup_platform();
  return g_fail_count == 0 ? 0 : 1;
}
