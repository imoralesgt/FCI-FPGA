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

#include "dma_s2mm.h"
#include "fsl.h"
#include "iic.h"
#include "intc.h"
#include "platform.h"
#include "registers.h"
#include "vga_dac.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "xstatus.h"

static int g_fail_count = 0;

static void check_u32(const char *name, u32 expected, u32 actual) {
  if (expected == actual) {
    xil_printf("  [PASS] %s = 0x%08x\r\n", name, actual);
  } else {
    xil_printf("  [FAIL] %s: wrote 0x%08x, read back 0x%08x\r\n", name, expected, actual);
    g_fail_count++;
  }
}

static void check_ok(const char *name, int ok) {
  if (ok) {
    xil_printf("  [PASS] %s\r\n", name);
  } else {
    xil_printf("  [FAIL] %s (no I2C ACK)\r\n", name);
    g_fail_count++;
  }
}

static void test_trigger_core(void) {
  xil_printf("-- trigger_core @ 0x%08x --\r\n", TRIGGER_CORE_BASEADDR);

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, 0x1234);
  check_u32("threshold", 0x1234, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET));

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_FALLING);
  check_u32("polarity", TRIGGER_CORE_POLARITY_FALLING,
            Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET));

  /* Mid-range values: no hardware clamping expected (valid range 2..256 / 1..4096). */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, 100);
  check_u32("delay", 100, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET));

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  check_u32("depth", 1024, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET));

  /* Restore the real operating point before leaving: this detector has an inverting preamp
   * ahead of the AFE, so real pulses arrive at the ADC rising above baseline (not dipping below
   * it, as the paper's raw non-inverted SiPM signal would) -- production use is polarity=rising. */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
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

static void test_vga_dac(void) {
  xil_printf("-- vga_dac (AD5697 @ 0x0D via axi_iic_0) --\r\n");

  if (Iic_Init(AXI_IIC_DEVICE_ID) != XST_SUCCESS) {
    xil_printf("  [FAIL] Iic_Init\r\n");
    g_fail_count++;
    return;
  }

  check_ok("VgaDac_Init (internal 2.5V reference)", VgaDac_Init());

  check_ok("VgaDac_SetGainFine(1x)", VgaDac_SetGainFine(AD8330_DEFAULT_GAIN_FINE_LINEAR));
  check_ok("VgaDac_SetGainCoarse(6x)", VgaDac_SetGainCoarse(AD8330_DEFAULT_GAIN_COARSE_LINEAR));
}

/* raw28 is a Q12.16 ap_ufixed<28,12> value (see fci_core.hpp): value = raw / 2^16. Printed by
 * hand since xil_printf has no floating-point support. */
static void print_psa(const char *label, u32 raw28) {
  u32 int_part = raw28 >> 16;
  u32 frac_part = ((raw28 & 0xFFFFu) * 10000u) / 65536u;
  xil_printf("  %s = %d.%04d (raw=0x%08x)\r\n", label, int_part, frac_part, raw28);
}

/* Confirmed ~30 events/s background rate at this location (Poisson): P(catch an event within
 * T=200ms) = 1 - exp(-30*0.2) ~= 99.75%. Iteration count is an approximation -- this design has
 * no hardware timer to calibrate wall-clock time precisely, so treat this as "a few hundred ms,"
 * not an exact figure. */
#define DMA_POLL_ITERS_200MS 500000
#define DMA_POLL_ITERS_2S (DMA_POLL_ITERS_200MS * 10) /* P ~= 1 - exp(-30*2) ~= 100% */

/* auto_restart + ap_start together (test_fci_core() above only set auto_restart, deliberately not
 * starting it without a live trigger source). Called once before test_dma_s2mm()/
 * test_threshold_sweep(), both of which need it already running. */
static void start_fci_core_realtime(void) {
  Xil_Out32(FCI_CORE_BASEADDR + FCI_CORE_AP_CTRL_OFFSET,
            FCI_CORE_AP_CTRL_AUTO_RESTART | FCI_CORE_AP_CTRL_START);
}

/* trigger_core's `above` comparator (trigger.vhd) is continuously live, re-evaluated every clk_i
 * cycle against whatever threshold is currently programmed -- it is NOT reset by writing a new
 * threshold. Any threshold write that lands below wherever `above` last settled produces no fresh
 * edge (no below->above transition, since it's already above), silently defeating the trigger
 * until the signal happens to dip and re-cross on its own. Flushing to max first, letting it
 * settle to '0', then writing the real value guarantees a clean starting state regardless of what
 * came before -- required before *every* threshold write in this file, not just the sweep. */
static void set_trigger_threshold(u32 threshold) {
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, 16383);
  for (volatile u32 i = 0; i < 1000; i++) {
  }
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, threshold);
}

static void test_dma_s2mm(void) {
  xil_printf("-- dma_s2mm: live capture, trigger_core -> fci_core -> BRAM --\r\n");

  /* depth MUST equal fci_core's N_SAMPLES (1024): trigger_core streams exactly this many beats
   * per capture, and fci_core's axis_to_fft loop reads exactly that many -- any mismatch either
   * hangs the pipeline or desyncs the next frame. Not a tunable test value like the others below. */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, 100);
  /* This detector has an inverting preamp ahead of the AFE, so real pulses arrive at the ADC
   * rising above baseline, not dipping below it. */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
  /* test_threshold_sweep() (called before this in main()) found the live baseline sitting in
   * ~9215-10239 (clean, reproducible across runs): no trigger at 10239, immediate trigger with
   * small/uniform PSA at 9215 and below. Set comfortably above that ceiling to reject baseline
   * and only catch genuine excursions -- still a first guess pending confirmation of where real
   * pulses actually land, e.g. via ILA on adc_data_i. */
  set_trigger_threshold(11000);

  if (!Dma_ResetCore()) {
    xil_printf("  [FAIL] Dma_ResetCore timed out\r\n");
    g_fail_count++;
    return;
  }

  DmaS2mm_ArmTransfer(FCI_RESULT_BRAM_BASEADDR, 8); /* 2 beats x 4 bytes: PSA_l, PSA_w */

  xil_printf("  [INFO] waiting for a live trigger (up to ~2s)...\r\n");
  DmaS2mmResult result = DmaS2mm_PollComplete(DMA_POLL_ITERS_2S);
  DmaS2mm_AckComplete();

  if (result == DMA_S2MM_TIMEOUT) {
    xil_printf("  [FAIL] timed out waiting for a triggered event (check threshold/detector)\r\n");
    g_fail_count++;
    return;
  }
  if (result == DMA_S2MM_ERROR) {
    xil_printf("  [FAIL] DMA transfer error (S2MM_DMASR = 0x%08x)\r\n",
               Xil_In32(AXI_DMA_BASEADDR + AXI_DMA_S2MM_DMASR_OFFSET));
    g_fail_count++;
    return;
  }

  /* axi_bram_ctrl_0 is only mapped into axi_dma_0's own address spaces, not into
   * microblaze_0/Data -- the CPU can't Xil_In32 it directly. Read it back via a fresh MM2S
   * transfer streamed into microblaze_0/S0_AXIS instead (see dma_s2mm.h). */
  DmaMm2s_ArmTransfer(FCI_RESULT_BRAM_BASEADDR, 8);
  u32 raw_l, raw_w;
  getfslx(raw_l, 0, FSL_DEFAULT);
  getfslx(raw_w, 0, FSL_DEFAULT);
  raw_l &= 0x0FFFFFFF;
  raw_w &= 0x0FFFFFFF;

  xil_printf("  [PASS] captured a live event:\r\n");
  print_psa("PSA_l", raw_l);
  print_psa("PSA_w", raw_w);

  if (raw_w == 0) {
    xil_printf("  [FAIL] PSA_w = 0, cannot compute FCI\r\n");
    g_fail_count++;
    return;
  }
  /* FCI = PSA_l/PSA_w; both share the same Q12.16 scale factor, so it cancels in the raw-code
   * ratio directly -- no need to convert to physical units first. */
  u64 fci_scaled = ((u64)raw_l * 10000ULL) / raw_w;
  xil_printf("  [INFO] FCI = PSA_l/PSA_w = %d.%04d\r\n", (u32)(fci_scaled / 10000),
             (u32)(fci_scaled % 10000));
}

/* Sweeps threshold from full-scale down to 0 with a short per-step timeout, reporting where
 * triggering starts (rising polarity: "signal >= threshold" is trivially true once threshold
 * drops to/below the live baseline, so it should go from "no trigger" to "triggers almost
 * immediately, small PSA" as threshold crosses the noise band). A real pulse should show up as a
 * threshold band that triggers with distinctly larger PSA_l/PSA_w than the noise-band captures
 * around it -- there's no scope on this setup to confirm the baseline directly, so this brackets
 * it from firmware instead. Depends on fci_core already running (start_fci_core_realtime() in
 * main(), before this and before test_dma_s2mm()). Uses set_trigger_threshold() (not a plain
 * Xil_Out32) so each step gets a clean, comparable reading -- see its comment for why. */
static void test_threshold_sweep(void) {
  xil_printf("-- threshold sweep (rising polarity, ~%d steps, short timeout each) --\r\n", 17);

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, 100);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);

  for (int step = 0; step <= 16; step++) {
    u32 threshold = (u32)(16383UL * (16 - step) / 16);
    set_trigger_threshold(threshold);

    if (!Dma_ResetCore()) {
      xil_printf("  [FAIL] Dma_ResetCore timed out\r\n");
      g_fail_count++;
      return;
    }
    DmaS2mm_ArmTransfer(FCI_RESULT_BRAM_BASEADDR, 8);

    DmaS2mmResult result = DmaS2mm_PollComplete(DMA_POLL_ITERS_200MS);
    DmaS2mm_AckComplete();

    if (result == DMA_S2MM_DONE) {
      DmaMm2s_ArmTransfer(FCI_RESULT_BRAM_BASEADDR, 8);
      u32 raw_l, raw_w;
      getfslx(raw_l, 0, FSL_DEFAULT);
      getfslx(raw_w, 0, FSL_DEFAULT);
      raw_l &= 0x0FFFFFFF;
      raw_w &= 0x0FFFFFFF;
      xil_printf("  threshold=%5d: TRIGGERED  PSA_l=0x%08x PSA_w=0x%08x\r\n", threshold, raw_l,
                 raw_w);
    } else if (result == DMA_S2MM_ERROR) {
      xil_printf("  threshold=%5d: DMA ERROR\r\n", threshold);
    } else {
      xil_printf("  threshold=%5d: no trigger\r\n", threshold);
    }
  }
}

/* Two fixed BRAM slots (8 bytes each -- PSA_l/PSA_w per event) inside the same 8KB
 * axi_bram_ctrl_0 region already used by the single-shot tests. Double-buffered so the ISR can
 * re-arm S2MM into the *other* slot before reading the just-completed one back out: without this,
 * a fast next trigger could start overwriting a slot while its MM2S readback of that same address
 * is still in flight. */
#define RESULT_BUF_A (FCI_RESULT_BRAM_BASEADDR)
#define RESULT_BUF_B (FCI_RESULT_BRAM_BASEADDR + 8)

/* Written only by dma_s2mm_isr(); read from main()'s idle loop. Single-word-aligned reads/writes
 * are atomic on MicroBlaze, and there's exactly one writer, so plain volatile (no lock) suffices. */
static volatile u32 g_event_count = 0;
static volatile u32 g_last_psa_l = 0;
static volatile u32 g_last_psa_w = 0;

/* Only ever touched from inside the ISR (single-threaded there), so this doesn't need volatile. */
static u32 g_write_buf = RESULT_BUF_A;

static void dma_s2mm_isr(void *callback_ref) {
  (void)callback_ref;

  DmaS2mm_AckComplete();

  /* Re-arm into the other slot for the *next* event before reading this one out -- keeps the
   * "blind window" where a new trigger could arrive but nothing is armed as short as possible. */
  u32 just_completed = g_write_buf;
  g_write_buf = (g_write_buf == RESULT_BUF_A) ? RESULT_BUF_B : RESULT_BUF_A;
  DmaS2mm_ArmTransfer(g_write_buf, 8);

  DmaMm2s_ArmTransfer(just_completed, 8);
  u32 raw_l, raw_w;
  getfslx(raw_l, 0, FSL_DEFAULT);
  getfslx(raw_w, 0, FSL_DEFAULT);
  g_last_psa_l = raw_l & 0x0FFFFFFF;
  g_last_psa_w = raw_w & 0x0FFFFFFF;
  g_event_count++;

  Xil_Out32(AXI_INTC_BASEADDR + AXI_INTC_IAR_OFFSET, INTC_DMA_S2MM_BIT);
}

/* Arms the pipeline once and returns immediately -- capture then runs entirely in the background
 * via dma_s2mm_isr(), re-arming itself after each event. main()'s loop is free to do anything else
 * (print, service UART, etc.) without blocking acquisition; this is the real-time property the
 * original block-design audit called for ("even if the MicroBlaze is attending UART character
 * parsing or any other slow tasks"). */
static void start_continuous_capture(void) {
  xil_printf("-- continuous interrupt-driven capture --\r\n");

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, 100);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
  set_trigger_threshold(11000); /* same first-guess as test_dma_s2mm(); see its comment */

  if (!Dma_ResetCore()) {
    xil_printf("  [FAIL] Dma_ResetCore timed out\r\n");
    g_fail_count++;
    return;
  }

  g_write_buf = RESULT_BUF_A;
  DmaS2mm_ArmTransfer(g_write_buf, 8);

  Intc_Init(INTC_DMA_S2MM_BIT, dma_s2mm_isr, NULL);

  xil_printf("  [INFO] armed -- events now serviced by interrupt in the background\r\n");
}

int main() {
  init_platform();

  xil_printf("\r\n=== FCI register bring-up test ===\r\n");
  test_trigger_core();
  test_fci_core();
  test_vga_dac();

  start_fci_core_realtime();
  test_threshold_sweep(); /* characterize the live baseline/threshold first, short timeouts */
  test_dma_s2mm();        /* single-shot "prove it end-to-end" capture, still using a guessed
                            * threshold until the sweep's result is plugged back in */

  xil_printf("=== %s (%d failure%s) ===\r\n", g_fail_count == 0 ? "TEST PASSED" : "TEST FAILED",
             g_fail_count, g_fail_count == 1 ? "" : "s");

  start_continuous_capture();

  u32 last_printed_count = 0;
  while (1) {
    u32 count = g_event_count; /* single volatile read, stable snapshot for this iteration */
    if (count != last_printed_count) {
      u32 psa_l = g_last_psa_l;
      u32 psa_w = g_last_psa_w;
      last_printed_count = count;

      xil_printf("  event #%d: ", count);
      if (psa_w == 0) {
        xil_printf("PSA_w = 0\r\n");
      } else {
        u64 fci_scaled = ((u64)psa_l * 10000ULL) / psa_w;
        xil_printf("FCI = %d.%04d\r\n", (u32)(fci_scaled / 10000), (u32)(fci_scaled % 10000));
      }
    }
  }
  /* Unreachable: continuous capture runs until reset, no cleanup_platform()/return path. */
}
