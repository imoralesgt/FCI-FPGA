/*
 * bringup.c
 *
 * Everything that brings the FCI acquisition chain up and proves it works: AXI4-Lite register
 * checks on trigger_core/fci_core, AD5697 VGA gain setup, the two interrupt-driven DMA pipelines
 * (fci_core PSA results on axi_dma_0, raw traces on axi_dma_1), automatic threshold calibration,
 * end-to-end capture tests, and the continuous acquisition loop that runs afterwards.
 *
 * Split out of main.c so main() stays a bare entry point. The whole sequence is behind a single
 * entry point, Bringup_Run() -- see bringup.h.
 *
 * Two historical diagnostics are preserved here behind compile-time flags, both defaulting off.
 * They answered the bring-up pulse-distortion question and are kept because they are the only
 * things that can reproduce it: VGA_BISECT_ENABLE sweeps the VGA fine-gain DAC, and
 * ENCODING_FOLD_DEMO_ENABLE renders a capture both corrected and as the pre-fix firmware read it.
 * See docs/log/ for the full account.
 */

#include "bringup.h"

#include "dma_s2mm.h"
#include "fsl.h"
#include "iic.h"
#include "intc.h"
#include "platform.h"
#include "acquisition.h"
#include "blr.h"
#include "fci_sink.h"
#include "psd.h"
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

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
  check_u32("polarity", TRIGGER_CORE_POLARITY_RISING,
            Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET));

  /* Mid-range values: no hardware clamping expected (valid range 2..256 / 1..4096). */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, 100);
  check_u32("delay", 100, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET));

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  check_u32("depth", 1024, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET));

  /* Polarity RISING. An earlier comment here claimed falling, on the strength of an oscilloscope
   * check of the ANALOG pulse -- but what matters is the digitized polarity after the front end's
   * inversions, and every clean capture since the gain was corrected shows pulses going UP from
   * baseline (baseline ~1873, peaks ~5900). The scope observation was one inversion earlier.
   *
   * Threshold PARKED at full scale on the way out, which is the important part. Until
   * start_fci_core_realtime() and start_raw_trace_pipeline() have both run, neither
   * axis_broadcaster_0 consumer can accept a beat -- and because the broadcaster is lockstep, a
   * trigger firing in that window leaves capture_engine stuck in STREAM holding beat 0. Leaving a
   * live threshold here (this test writes 0x1234 = 4660, which real pulses cross every time) makes
   * that a race against however long the I2C in test_vga_dac() happens to take. With RISING
   * polarity, `above` requires adc_data >= 16383, which never happens, so no capture can start
   * until calibrate_threshold() deliberately lowers it with the whole pipeline already armed. */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, 16383);
}

/* fci_sink replaces axi_dma_0 on fci_core's result path. Kept as a switch rather than a deletion
 * because the block design is what decides which of the two exists: set this to 0 to build against
 * a BD that still carries axi_dma_0, 1 for one that carries fci_sink. Nothing else in this file
 * needs touching to move between them. */
#define FCI_RESULT_VIA_FCI_SINK 0

static void test_blr_core(void) {
  xil_printf("-- blr_core registers --\r\n");
  check_ok("blr register write/read", Blr_SelfTest(BLR_CORE_BASEADDR));

  /* The estimator seeds from the first sample after reset, so by the time firmware runs it has
   * long since converged on whatever the input is doing. A baseline of exactly 0 means it never
   * saw a sample -- the likeliest cause being that the ADC stream is not reaching it at all. */
  s32 baseline = Blr_GetBaseline(BLR_CORE_BASEADDR);
  xil_printf("  [INFO] blr baseline = %d, gate %s\r\n", (int)baseline,
             Blr_GateOpen(BLR_CORE_BASEADDR) ? "open" : "shut");
  check_ok("blr baseline is tracking (nonzero)", baseline != 0);
}

static void test_psd_core(void) {
  xil_printf("-- psd_core registers --\r\n");
  check_ok("psd register write/read", Psd_SelfTest(PSD_CORE_BASEADDR));
  Psd_Clear(PSD_CORE_BASEADDR);
  check_ok("psd FIFO empty after clear", Psd_Level(PSD_CORE_BASEADDR) == 0);
}

#if FCI_RESULT_VIA_FCI_SINK
static void test_fci_sink(void) {
  xil_printf("-- fci_sink registers --\r\n");
  FciSink_SetWatermark(FCI_SINK_BASEADDR, 7);
  check_ok("fci_sink watermark write/read",
           Xil_In32(FCI_SINK_BASEADDR + FCI_SINK_WATERMARK_OFFSET) == 7);
  FciSink_Clear(FCI_SINK_BASEADDR);
  check_ok("fci_sink FIFO empty after clear", FciSink_Level(FCI_SINK_BASEADDR) == 0);
}
#endif

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

  check_ok("VgaDac_SetGainFine(1x -> code 819)", VgaDac_SetGainFine(AD8330_DEFAULT_GAIN_FINE_LINEAR));
  check_ok("VgaDac_SetGainCoarse(6x -> code 765)",
           VgaDac_SetGainCoarse(AD8330_DEFAULT_GAIN_COARSE_LINEAR));
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
/* For waits that must catch a REAL detector event with no source present -- background only (NORM
 * + cosmics) runs at a few Hz at most, where 2s is not a safe margin. */
#define DMA_POLL_ITERS_10S (DMA_POLL_ITERS_200MS * 50)

/* auto_restart + ap_start together (test_fci_core() above only set auto_restart, deliberately not
 * starting it without a live trigger source). Called once before calibrate_threshold()/
 * test_dma_s2mm(), both of which need it already running. */
static void start_fci_core_realtime(void) {
  Xil_Out32(FCI_CORE_BASEADDR + FCI_CORE_AP_CTRL_OFFSET,
            FCI_CORE_AP_CTRL_AUTO_RESTART | FCI_CORE_AP_CTRL_START);
}

/* --- Raw-trace capture (axi_dma_1), continuously interrupt-driven from as early in main() as
 * possible -- see start_raw_trace_pipeline() for why this can't wait until "someone actually
 * wants a trace." ---------------------------------------------------------------------------- */

/* axi_bram_ctrl_1 is a dedicated 8KB block (2048 words x 32-bit, confirmed in the exported .hwh)
 * just for these two double-buffers -- 2048 samples x 2 bytes x 2 buffers = 8192 bytes, exactly
 * filling it with no slack, which is fine since nothing else shares this BRAM. Also sets the size
 * of the local readout copy in test_raw_trace_capture() below (4KB, comfortably inside the now
 * 64KB microblaze_0_local_memory). */
#define RAW_TRACE_MAX_SAMPLES 2048
#define RAW_TRACE_BUF_A (RAW_TRACE_BRAM_BASEADDR)
#define RAW_TRACE_BUF_B (RAW_TRACE_BRAM_BASEADDR + RAW_TRACE_MAX_SAMPLES * 2)

static u32 g_raw_write_buf = RAW_TRACE_BUF_A; /* ISR-only */
/* Published by the ISR once at least one capture has completed; 0 = none yet. */
static volatile u32 g_raw_ready_buf = 0;
static volatile u32 g_raw_ready_depth = 0;
/* Total captures axi_dma_1 has ever completed -- lets callers detect "at least one *new* capture
 * happened since I last checked," which g_raw_ready_buf alone can't (it stays non-zero forever
 * once set). Used by set_trigger_threshold() below to wait out its own self-inflicted trigger. */
static volatile u32 g_raw_event_count = 0;

static void service_dma1_event(void) {
  DmaS2mm_AckComplete(AXI_DMA_1_BASEADDR);

  u32 just_completed = g_raw_write_buf;
  u32 depth = Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET);
  if (depth == 0 || depth > RAW_TRACE_MAX_SAMPLES)
    depth = RAW_TRACE_MAX_SAMPLES; /* guard the fixed-size buffers above */

  /* Re-arm into the other slot before publishing/reading this one, same reasoning as the PSA
   * double-buffer below. */
  g_raw_write_buf = (g_raw_write_buf == RAW_TRACE_BUF_A) ? RAW_TRACE_BUF_B : RAW_TRACE_BUF_A;
  DmaS2mm_ArmTransfer(AXI_DMA_1_BASEADDR, g_raw_write_buf, depth * 2);

  g_raw_ready_buf = just_completed;
  g_raw_ready_depth = depth;
  g_raw_event_count++;
}

/* trigger_core's `above` comparator (trigger.vhd) is continuously live, re-evaluated every clk_i
 * cycle against whatever threshold is currently programmed, and a threshold write never resets
 * it. Reconfiguring is therefore a two-step operation (flush to a known state, then write the
 * real value) so the outcome doesn't depend on whatever `above` happened to be left at by the
 * *previous* threshold. Flushing first MAY cross the active polarity's trigger edge as a side
 * effect (e.g. falling polarity: if `above` was `1` under the old threshold, forcing it to
 * `16383` drives a genuine 1->0 transition) -- but only when the old threshold's `above` state
 * and the new one's actually differ. A monotonic sweep that never crosses the live baseline (as
 * calibrate_threshold()'s downward probe does) can go many calls in a row with `above` sitting at
 * a constant 0 the whole time, so a spurious capture on every single call is NOT guaranteed --
 * confirmed on hardware (2026-08-17 sweep: 6 of 17 steps saw no self-inflicted capture at all,
 * which is expected, not a fault).
 *
 * Whether or not this flush happens to fire a capture, it doesn't matter to correctness: callers
 * needing a guaranteed-fresh, uncontaminated trace (test_raw_trace_capture()) wait for
 * g_raw_event_count to advance from their *own* entry value, not from anything recorded here. */
static void set_trigger_threshold(s32 threshold) {
  /* Park at the top of the signed range first so nothing can fire mid-update. 32767 is the most
   * positive 16-bit signed level, unreachable by any real sample. */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, 32767);
  for (volatile u32 i = 0; i < 1000; i++) {
  }
  /* Written as a 16-bit two's-complement pattern; the core's threshold register is signed and
   * masks to its own width, so a negative level round-trips correctly. */
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, (u32)threshold & 0xFFFFu);
}

/* --- axi_dma_0 (PSA result) double buffering. test_dma_s2mm() below owns
 * axi_dma_0 directly (polled, one-shot) until start_continuous_capture() hands it over to
 * service_dma0_event() -- its interrupt is intentionally NOT enabled until then, so the two never
 * fight over the same DMA channel. --------------------------------------------------------- */
#define RESULT_BUF_A (FCI_RESULT_BRAM_BASEADDR)
#define RESULT_BUF_B (FCI_RESULT_BRAM_BASEADDR + 8)

static volatile u32 g_event_count = 0;
static volatile u32 g_last_psa_l = 0;
static volatile u32 g_last_psa_w = 0;
static u32 g_write_buf = RESULT_BUF_A; /* ISR-only */

static void service_dma0_event(void) {
  DmaS2mm_AckComplete(AXI_DMA_BASEADDR);

  u32 just_completed = g_write_buf;
  g_write_buf = (g_write_buf == RESULT_BUF_A) ? RESULT_BUF_B : RESULT_BUF_A;
  DmaS2mm_ArmTransfer(AXI_DMA_BASEADDR, g_write_buf, 8);

  DmaMm2s_ArmTransfer(AXI_DMA_BASEADDR, just_completed, 8);
  u32 raw_l, raw_w;
  getfslx(raw_l, 0, FSL_DEFAULT);
  getfslx(raw_w, 0, FSL_DEFAULT);
  g_last_psa_l = raw_l & 0x0FFFFFFF;
  g_last_psa_w = raw_w & 0x0FFFFFFF;
  g_event_count++;
}

/* Single registered handler for MicroBlaze's one external interrupt line -- axi_intc funnels
 * every enabled source into it, so the handler must check IPR to see which is actually pending
 * rather than assume. */
static void intc_isr(void *callback_ref) {
  (void)callback_ref;
  u32 ipr = Xil_In32(AXI_INTC_BASEADDR + AXI_INTC_IPR_OFFSET);

  if (ipr & INTC_DMA_1_S2MM_BIT) {
    service_dma1_event();
    Xil_Out32(AXI_INTC_BASEADDR + AXI_INTC_IAR_OFFSET, INTC_DMA_1_S2MM_BIT);
  }
  if (ipr & INTC_DMA_S2MM_BIT) {
    service_dma0_event();
    Xil_Out32(AXI_INTC_BASEADDR + AXI_INTC_IAR_OFFSET, INTC_DMA_S2MM_BIT);
  }
}

/* Arms axi_dma_1's first raw-trace buffer and brings up interrupts with only its bit enabled.
 * Must run before any real trigger can occur: axis_broadcaster_0 (trigger_core's raw output,
 * split to both fci_core and axi_dma_1) won't accept a beat on EITHER branch until BOTH
 * consumers are ready, so an unarmed axi_dma_1 stalls fci_core too, not just the raw-trace tap --
 * that's the deadlock the sweep/test_dma_s2mm() hit before this existed. axi_dma_0's interrupt is
 * added later, by start_continuous_capture(), via Intc_EnableAdditional() rather than a second
 * Intc_Init() call. */
static void start_raw_trace_pipeline(void) {
  if (!Dma_ResetCore(AXI_DMA_1_BASEADDR)) {
    xil_printf("  [FAIL] Dma_ResetCore (axi_dma_1) timed out\r\n");
    g_fail_count++;
    return;
  }

  u32 depth = Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET);
  if (depth == 0 || depth > RAW_TRACE_MAX_SAMPLES)
    depth = RAW_TRACE_MAX_SAMPLES;

  g_raw_write_buf = RAW_TRACE_BUF_A;
  DmaS2mm_ArmTransfer(AXI_DMA_1_BASEADDR, g_raw_write_buf, depth * 2);

  Intc_Init(INTC_DMA_1_S2MM_BIT, intc_isr, NULL);
}

/* Streams `depth` samples out of the raw-trace BRAM starting at buf_addr and prints them as
 * "RAW,<depth>" followed by one sample per line -- shared by test_raw_trace_capture() and the
 * early-event dump in main()'s loop below. Same CPU-can't-reach-the-BRAM-directly constraint as
 * the PSA path: read back via MM2S, stream index 1 (microblaze_0/S1_AXIS, not S0_AXIS). axi_dma_1's
 * S2MM stream is 16-bit but its memory-side bus is 32-bit (confirmed in the exported .hwh), so it
 * packed two samples per 32-bit word on the way in; unpack low half first (earlier sample), then
 * high half. Copies into a local array before printing, not interleaved: the ISR keeps re-arming
 * into the *other* slot independently of this readout, but if a slow UART dump were interleaved
 * with the FSL reads, two more real events completing mid-print could let the ISR wrap back around
 * and overwrite the very buffer still being printed from. Copy first, print after, and that race
 * can't happen. */
/* SIGNED. blr_core restores the baseline to zero and emits signed samples, so a quiet trace sits
 * around 0 and undershoot is genuinely negative. Reading these as unsigned would render every
 * below-baseline sample as ~65000 and turn the noise statistics into nonsense. */
static s16 g_trace[RAW_TRACE_MAX_SAMPLES];

/* Streams `depth` samples out of the raw-trace BRAM at buf_addr into g_trace. Kept separate from
 * printing so calibrate_threshold() can analyse a trace without dumping it over the UART. */
static void read_raw_trace(u32 buf_addr, u32 depth) {
  DmaMm2s_ArmTransfer(AXI_DMA_1_BASEADDR, buf_addr, depth * 2);
  u32 copied = 0;
  while (copied < depth) {
    u32 word;
    getfslx(word, 1, FSL_DEFAULT);
    g_trace[copied++] = (s16)(word & 0xFFFF);
    if (copied < depth)
      g_trace[copied++] = (s16)((word >> 16) & 0xFFFF);
  }
}

static void print_raw_trace(u32 buf_addr, u32 depth) {
  read_raw_trace(buf_addr, depth);

  xil_printf("RAW,%d\r\n", depth);
  for (u32 i = 0; i < depth; i++)
    xil_printf("%d\r\n", g_trace[i]);
}

/* When the raw-trace path yields nothing, "no capture" alone cannot distinguish the three very
 * different causes: the trigger never fired, or it fired but axi_dma_1 never completed (the
 * lockstep-broadcaster stall), or it completed but the interrupt never reached us. These registers
 * separate them:
 *   S2MM_DMASR bit0 Halted, bit1 Idle, bit12 IOC_Irq, bits 4..6 error flags.
 *     Idle=1 with no IOC means armed and waiting -- the trigger genuinely never fired.
 *     Idle=0 means a transfer is in flight, i.e. beats are stuck mid-stream.
 *   INTC ISR raw status / IER enables / IPR pending. IOC set in DMASR while the matching IPR bit
 *     never clears points at the interrupt path, not the datapath. */
static void report_raw_path_state(void) {
  xil_printf("  [DIAG] dma1 S2MM_DMASR=0x%08x  MM2S_DMASR=0x%08x  raw_events=%d\r\n",
             Xil_In32(AXI_DMA_1_BASEADDR + AXI_DMA_S2MM_DMASR_OFFSET),
             Xil_In32(AXI_DMA_1_BASEADDR + AXI_DMA_MM2S_DMASR_OFFSET), g_raw_event_count);
  xil_printf("  [DIAG] intc ISR=0x%08x IER=0x%08x IPR=0x%08x MER=0x%08x (dma1 bit=0x%08x)\r\n",
             Xil_In32(AXI_INTC_BASEADDR + AXI_INTC_ISR_OFFSET),
             Xil_In32(AXI_INTC_BASEADDR + AXI_INTC_IER_OFFSET),
             Xil_In32(AXI_INTC_BASEADDR + AXI_INTC_IPR_OFFSET),
             Xil_In32(AXI_INTC_BASEADDR + AXI_INTC_MER_OFFSET), INTC_DMA_1_S2MM_BIT);
}

/* --- Automatic threshold calibration ------------------------------------------------------
 *
 * Every previous threshold in this file was a hand-picked constant, and each one went stale the
 * moment anything upstream changed -- ADC encoding, VGA gain, polarity. Rather than guess again,
 * measure the live baseline and derive the threshold from it, which is also what the paper
 * specifies (Morales et al. section 4.2.2: "a threshold set at four standard deviations of the
 * baseline Gaussian noise").
 *
 * Why not 4 sigma here: the paper's figure is for offline analysis of already-recorded traces.
 * This is a live comparator evaluating a new sample every 20 ns, so the false-trigger RATE is what
 * matters, not the per-sample probability. At 50 Msps, 4 sigma (P ~ 3e-5 one-sided) would fire
 * ~1500 times/second on noise alone -- swamping a ~30 Hz real event rate. Each additional sigma
 * cuts that by roughly two orders of magnitude, so THRESHOLD_SIGMA_MULT below is the real tuning
 * knob: raise it if noise triggers persist, lower it if genuine small events are being missed.
 *
 * Baseline statistics are taken from the PRE-TRIGGER portion of a capture. trigger_core's delay
 * line puts TRIGGER_DELAY samples of pre-trigger history at the start of every trace, so those
 * samples are quiet baseline recorded before whatever caused the trigger -- true even when the
 * trigger itself fired on noise, which is exactly the situation this recovers from. */
#define TRIGGER_DELAY 100

/* Pre-trigger samples included in both PSD gates. Kept here next to TRIGGER_DELAY because the gate
 * start is derived from the two together, and report_gate_scan() must use the same arithmetic
 * psd_core does or the scan would describe a different window than the one being measured. */
#define PSD_PRE_GATE_SAMPLES 32
#define BASELINE_SAMPLES 64 /* comfortably inside the TRIGGER_DELAY pre-trigger region */
#define THRESHOLD_SIGMA_MULT 8

/* SIGNED: a level on the zero-centred restored stream, so a small positive number in normal
 * operation rather than a large offset-binary code. */
static s32 g_calibrated_threshold; /* 0 until calibrate_threshold() succeeds */

/* Measured baseline sigma from the last successful calibration. Retained because blr_core's gate
 * threshold is derived from it (see Blr_GateThresholdForSigma): the gate has to sit clear of the
 * noise, and the only honest source for "how much noise" is the same measurement the trigger
 * threshold already uses. 0 means calibration has not run, and Acq_Configure() falls back to the
 * hardware reset default rather than deriving a threshold from a number it does not have. */
static u32 g_last_sigma;

static u32 isqrt_u32(u32 v) {
  u32 r = 0, bit = 1UL << 30;
  while (bit > v)
    bit >>= 2;
  while (bit) {
    if (v >= r + bit) {
      v -= r + bit;
      r = (r >> 1) + bit;
    } else {
      r >>= 1;
    }
    bit >>= 2;
  }
  return r;
}

/* Sweeps the threshold down from full scale until the always-armed raw-trace pipeline produces a
 * capture, then measures baseline mean/sigma from that trace's pre-trigger region and sets the
 * threshold to mean + THRESHOLD_SIGMA_MULT*sigma. Rising polarity throughout: the digitized pulses
 * on this AFE go UP from baseline (confirmed in every clean raw dump), whatever the analog
 * polarity is before the front end's inversions.
 *
 * The downward sweep is what makes this self-starting: a threshold above everything never fires,
 * and as it descends past the top of the noise distribution captures begin -- so the first hit
 * lands near the noise peak without needing to know the signal levels beforehand. */
/* Locates the baseline by finding which thresholds the NOISE crosses, instead of waiting for the
 * detector to produce an event.
 *
 * This replaces a downward probe sweep that waited ~100 ms at each of 62 steps for a real pulse to
 * cross. Background here runs ~30 cps, so that dwell would have caught an event ~95% of the time
 * per in-range step -- the sweep's real problem was not event rarity (an earlier version of this
 * comment blamed that, from a 10x-low rate estimate) but that with RISING polarity the trigger
 * response is NOT monotonic in threshold.
 *
 *   T well below baseline : `above` is permanently 1, so no rising edge ever occurs -> NEVER fires
 *   T within the noise band : noise crosses constantly -> fires at kHz
 *   T above the noise, below the pulse peaks : fires only on real events -> a few Hz here
 *   T above the pulse peaks : never fires
 *
 * Only that third window responds to real events, and it is narrow: baseline ~1861 up to the peak
 * of whatever event arrives, and background pulses are mostly small (one captured trace peaked just
 * 107 counts above baseline). Thresholds high in that window are only crossed by the rare large
 * event, so a descending sweep spends most of its steps in territory nothing reaches. Worse, the
 * captures it did report were usually stale ones draining rather than genuine triggers -- which is
 * why it kept announcing first captures at thresholds like 14208 and 15488 on traces that never
 * exceed 3700.
 *
 * The noise band has none of those problems: it is always present, independent of the detector, and
 * fires at kHz so a few milliseconds of dwell is decisive. Sweeping UP from 0 and recording the
 * first and last threshold that fire brackets it directly. Returns 0 if nothing ever fires, which
 * now genuinely means the raw path is broken rather than "no event happened to arrive". */
static int find_noise_band(u32 *out_lo, u32 *out_hi) {
  /* STEP must be well UNDER the noise width or the scan steps straight over the band and reports
   * nothing. Measured sigma on a quiet baseline (background only, no source) is ~7 counts, so the
   * band is only about +/-4 sigma ~= 56 counts wide -- an earlier STEP of 64 was wider than the
   * whole band and missed it outright. The sigma ~50 that motivated that value was itself inflated:
   * at higher event rates the pre-trigger window catches tails of earlier pulses. 8 samples the
   * band several times over even in the quietest case. */
  const u32 STEP = 8;
  const u32 DWELL = DMA_POLL_ITERS_200MS / 100; /* ~ms. A capture streams in ~20-40 us, and in-band
                                                 * noise crosses at kHz, so this is many times over
                                                 * what a real band needs -- while staying far too
                                                 * short to catch the few-Hz detector events, which
                                                 * is what keeps the band unambiguous. */
  /* Stop once the band has clearly ended rather than scanning all 2048 steps to full scale: the
   * baseline sits low (~1855), so this returns in a few hundred ms instead of tens of seconds. */
  const u32 MISS_LIMIT = 8; /* 8 * STEP = 64 counts of silence above the top edge */

  u32 lo = 0, hi = 0, misses = 0;
  int found = 0;

  for (u32 t = 0; t <= 16383; t += STEP) {
    /* Order matters. Snapshotting the counter BEFORE programming the threshold lets a capture that
     * was already in flight under the PREVIOUS threshold land inside this step's dwell and be
     * scored as a hit here. At 30 cps with the pipeline streaming continuously that happens
     * regularly, and it reported bands like "0..0" -- threshold 0 can never fire with RISING
     * polarity, since `above` is then permanently 1. Program first, let anything in flight retire,
     * then snapshot, then dwell. */
    set_trigger_threshold(t);
    for (volatile u32 q = 0; q < 20000; q++) {
    }
    u32 before = g_raw_event_count;
    for (u32 w = 0; w < DWELL && g_raw_event_count == before; w++) {
    }
    if (g_raw_event_count != before) {
      if (!found) {
        lo = t;
        found = 1;
      }
      hi = t;
      misses = 0;
    } else if (found && ++misses >= MISS_LIMIT) {
      break;
    }
  }

  *out_lo = lo;
  *out_hi = hi;
  return found;
}

static int calibrate_threshold(void) {
  xil_printf("-- threshold calibration (auto, %d sigma over baseline) --\r\n",
             THRESHOLD_SIGMA_MULT);

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, TRIGGER_DELAY);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);

  u32 band_lo, band_hi;
  if (!find_noise_band(&band_lo, &band_hi)) {
    xil_printf("  [FAIL] no noise band found -- cannot calibrate\r\n");
    report_raw_path_state();
    g_fail_count++;
    return 0;
  }
  /* Band centre is the baseline, to within the scan step -- a useful cross-check against the mean
   * computed from the trace below, since the two are measured completely differently. */
  u32 band_mid = (band_lo + band_hi) / 2;
  xil_printf("  [INFO] noise band spans thresholds %d..%d (centre %d)\r\n", band_lo, band_hi,
             band_mid);

  /* Park at the CENTRE of the band, not its top edge. The edges are by definition where crossings
   * are rarest, so parking there can find the band and then wait out the timeout without a single
   * capture; the centre crosses at kHz and returns immediately. */
  u32 count_before = g_raw_event_count;
  set_trigger_threshold(band_mid);
  u32 waited;
  for (waited = 0; g_raw_event_count == count_before && waited < DMA_POLL_ITERS_2S; waited++) {
  }
  if (g_raw_event_count == count_before) {
    xil_printf("  [FAIL] noise band found but no capture at %d -- cannot calibrate\r\n", band_mid);
    report_raw_path_state();
    g_fail_count++;
    return 0;
  }

  u32 depth = g_raw_ready_depth;
  if (depth > BASELINE_SAMPLES) /* only the pre-trigger region is guaranteed quiet */
    depth = BASELINE_SAMPLES;
  read_raw_trace(g_raw_ready_buf, g_raw_ready_depth);

  /* Signed throughout: with the baseline restored to zero the mean is near 0 and can legitimately
   * be negative, so an unsigned accumulator would wrap on the first below-zero sample. */
  s32 sum = 0;
  for (u32 i = 0; i < depth; i++)
    sum += g_trace[i];
  s32 mean = sum / (s32)depth;

  /* 64-bit accumulator: if the "pre-trigger" window is not actually quiet (a pulse landing early,
   * or a capture triggered mid-event), a single outlier contributes d*d up to ~2.7e8 and a u32
   * would wrap, producing a silently wrong sigma and thus a nonsense threshold. */
  u64 var_acc = 0;
  for (u32 i = 0; i < depth; i++) {
    s32 d = (s32)g_trace[i] - mean;
    var_acc += (u64)((s64)d * (s64)d);
  }
  u32 sigma = isqrt_u32((u32)(var_acc / depth));
  if (sigma == 0) /* a perfectly flat window would otherwise collapse the margin to zero */
    sigma = 1;

  g_last_sigma = sigma;
  g_calibrated_threshold = mean + THRESHOLD_SIGMA_MULT * sigma;
  xil_printf("  [INFO] baseline mean=%d sigma=%d over %d pre-trigger samples\r\n", mean, sigma,
             depth);
  xil_printf("  [PASS] threshold set to %d (mean + %d sigma)\r\n", g_calibrated_threshold,
             THRESHOLD_SIGMA_MULT);
  set_trigger_threshold(g_calibrated_threshold);
  return 1;
}

/* Reads back the next NEW capture the background pipeline produces, instead of arming/waiting
 * itself -- axi_dma_1 is already continuously re-arming via service_dma1_event(). Deliberately
 * waits for g_raw_event_count to advance from its value *at entry*, not just for g_raw_ready_buf
 * to be non-zero: by the time this runs, set_trigger_threshold() has already been called several
 * times (by calibrate_threshold()/test_dma_s2mm() above), and some of those calls may have left
 * a stale capture (spurious or otherwise) sitting in g_raw_ready_buf -- so a plain "is it
 * non-zero" check could return old data instead of a fresh one. Waiting for the count to move
 * guarantees whatever we read was captured after this function started looking. */
/* Cumulative charge as a function of long-gate length, over the trace currently in g_trace.
 *
 * This exists because the long gate cannot be derived from the pulse decay alone. The AFE's
 * response undershoots after a pulse, so past some length the gate starts integrating NEGATIVE
 * signal and the measured charge falls again. The first hardware run showed 14% of events with
 * El < Es -- impossible for a positive pulse, since the long gate contains the short one -- which
 * is that undershoot being included.
 *
 * The useful long gate is the one at the maximum of this curve: long enough to collect the tail,
 * short enough to stop before the undershoot. Printing the curve turns that into a measurement
 * instead of a guess. */
/* Prints a value scaled by 10000 as a decimal, e.g. -5000 -> "-0.5000".
 *
 * The sign has to be handled before the split, not after: in C, -5000/10000 truncates toward zero
 * and gives 0, so printing the quotient and remainder separately would render -0.5000 as "0.5000"
 * -- correct magnitude, silently wrong sign. It only shows up for values between -1 and 0, which
 * is exactly the range a marginally-negative tail integral lands in. */
static void print_fixed4(s32 scaled) {
  s32 mag = (scaled < 0) ? -scaled : scaled;
  if (scaled < 0)
    xil_printf("-");
  xil_printf("%d.%04d", mag / 10000, mag % 10000);
}

static void report_gate_scan(u32 depth) {
  u32 gate_start = (TRIGGER_DELAY > PSD_PRE_GATE_SAMPLES) ? TRIGGER_DELAY - PSD_PRE_GATE_SAMPLES : 0;
  xil_printf("# [SCAN] cumulative charge vs long-gate length (gate starts at sample %d)\r\n",
             gate_start);
  for (u32 len = 50; len <= 600; len += 50) {
    if (gate_start + len > depth)
      break;
    s32 acc = 0;
    for (u32 i = gate_start; i < gate_start + len; i++)
      acc += g_trace[i];
    xil_printf("# [SCAN] len=%3d  charge=%d\r\n", len, acc);
  }
}

static void test_raw_trace_capture(void) {
  xil_printf("-- raw_trace: latest capture via axi_dma_1 --\r\n");

  u32 count_before = g_raw_event_count;
  u32 waited;
  for (waited = 0; g_raw_event_count == count_before && waited < DMA_POLL_ITERS_2S; waited++) {
  }
  if (g_raw_event_count == count_before) {
    xil_printf("  [FAIL] no new raw trace captured (check threshold/detector)\r\n");
    report_raw_path_state();
    g_fail_count++;
    return;
  }

  xil_printf("  [PASS] captured %d raw samples:\r\n", g_raw_ready_depth);
  print_raw_trace(g_raw_ready_buf, g_raw_ready_depth);
  report_gate_scan(g_raw_ready_depth);
}

/* --- VGA fine-gain bisect (diagnostic) -----------------------------------------------------
 *
 * Settles the last open question from bring-up: which change actually removed the amplitude-
 * dependent pulse distortion (fast overshoot spike, flat plateau, undershoot, slow settle).
 *
 * The fine-gain DAC spent that whole period at code 0 -- the coarse channel's LOGARITHMIC formula
 * was being applied to the linear fine channel, and log10(1.0) = 0. The competing explanation, ADC
 * bus capture skew, was ruled out by measurement rather than argument: system_ila_0 samples the
 * same ADC pads through its own fabric registers that the IOB attribute never touched, carrying
 * 3.551 ns of skew -- MORE than trigger_core's 2.464 ns before that fix -- and it shows clean
 * pulses. If 3.551 ns does not corrupt the bus, 2.464 ns did not either.
 *
 * That leaves inference, not proof, because both fixes reached hardware in the same build. This
 * reproduces the historical condition directly: same bitstream, same everything, one variable.
 *
 * Reported per code, so the answer does not depend on eyeballing dumps:
 *   mean/sigma - from calibrate_threshold(). How the baseline and its noise scale with the code
 *                also identifies the control law: proportional => AD8330 VMAG (the linear,
 *                output-magnitude pin, nominal 0.5 V, which is what the fine formula's
 *                V = 0.5 * gain implies), dB-like => VDBS.
 *   amp        - peak above baseline.
 *   plateau    - samples within 3 sigma of the peak. A clean pulse turns over within a few
 *                samples; a clipped/overloaded one holds a flat top. This is the direct
 *                signature of the artifact and the number that decides the question.
 *   undershoot - how far below baseline the trace dips after the peak: the other half of the
 *                overload-recovery signature.
 *
 * Set VGA_BISECT_ENABLE to 0 to drop this from normal operation once the question is closed. */
#define VGA_BISECT_ENABLE 0 /* answered: see vga_dac.h HISTORY. Flip to 1 to re-run. */

/* Codes spanning the fine channel: 0 is the historical bug, 819 the correct value for gain 1.0
 * (V = 0.5 V), 1638 the top of the documented range (gain 2.0). 205/410 fill in between so the
 * control law and the onset of any distortion are both visible rather than just its endpoints. */
#if VGA_BISECT_ENABLE

static const u16 VGA_BISECT_CODES[] = {0, 205, 410, 819, 1638};
#define VGA_BISECT_N (sizeof(VGA_BISECT_CODES) / sizeof(VGA_BISECT_CODES[0]))

/* Full 1024-sample dumps only at the two codes that answer the question; the rest report metrics
 * only, to keep the UART log readable. */
static int vga_bisect_should_dump(u16 code) { return code == 0 || code == 819; }

static void report_trace_metrics(u32 depth, s32 mean, u32 sigma) {
  /* Seeded from a real sample rather than 0: on a signed, zero-centred stream a fixed unsigned
   * seed is not a valid starting extreme -- an all-negative trace would report a peak of 0 that
   * no sample ever attained. */
  s32 peak = g_trace[0];
  u32 peak_idx = 0;
  for (u32 i = 0; i < depth; i++) {
    if (g_trace[i] > peak) {
      peak = g_trace[i];
      peak_idx = i;
    }
  }

  s32 plateau_floor = peak - 3 * (s32)sigma;
  u32 plateau = 0;
  for (u32 i = 0; i < depth; i++)
    if (g_trace[i] >= plateau_floor)
      plateau++;

  s32 trough = g_trace[peak_idx];
  for (u32 i = peak_idx; i < depth; i++)
    if (g_trace[i] < trough)
      trough = g_trace[i];

  int amp = (int)(peak - mean);
  int undershoot = (int)(mean - trough);

  xil_printf("  [DATA] peak=%d @%d  amp=%d  plateau=%d samples  undershoot=%d\r\n", peak, peak_idx,
             amp, plateau, undershoot);
}



static void test_vga_fine_bisect(void) {
  xil_printf("\r\n-- VGA fine-gain bisect: is fine code 0 the cause of the distortion? --\r\n");

  for (u32 k = 0; k < VGA_BISECT_N; k++) {
    u16 code = VGA_BISECT_CODES[k];
    xil_printf("\r\n  === fine DAC code %d%s ===\r\n", code,
               code == 0 ? " (the historical bug)" : (code == 819 ? " (correct, gain 1.0)" : ""));

    if (!VgaDac_SetFineCodeRaw(code)) {
      xil_printf("  [FAIL] I2C write failed\r\n");
      g_fail_count++;
      continue;
    }
    /* Let the AD8330 settle at the new operating point before measuring anything. */
    for (volatile u32 d = 0; d < DMA_POLL_ITERS_200MS; d++) {
    }

    /* Re-derives the threshold at THIS gain, so each code is triggered on its own noise floor
     * rather than a level tuned for a different one -- otherwise a quieter setting simply stops
     * triggering and looks deceptively "clean". Also prints the mean/sigma this test wants. */
    if (!calibrate_threshold()) {
      xil_printf("  [INFO] no captures at this gain -- signal too small to trigger\r\n");
      continue;
    }

    /* calibrate_threshold() left the threshold at mean + 8 sigma, so this waits for a genuine
     * detector pulse rather than a noise crossing -- which is the whole point, since the shape of
     * that pulse is what the bisect is measuring. Background-only rates make this the slow step. */
    u32 count_before = g_raw_event_count;
    u32 waited;
    for (waited = 0; g_raw_event_count == count_before && waited < DMA_POLL_ITERS_10S; waited++) {
    }
    if (g_raw_event_count == count_before) {
      xil_printf("  [INFO] no event captured within ~10s at this gain\r\n");
      continue;
    }

    u32 depth = g_raw_ready_depth;
    read_raw_trace(g_raw_ready_buf, depth);

    u32 stat_n = (depth > BASELINE_SAMPLES) ? BASELINE_SAMPLES : depth;
    s32 sum = 0;
    for (u32 i = 0; i < stat_n; i++)
      sum += g_trace[i];
    s32 mean = sum / (s32)stat_n;
    u64 var_acc = 0;
    for (u32 i = 0; i < stat_n; i++) {
      s32 d = (s32)g_trace[i] - mean;
      var_acc += (u64)((s64)d * (s64)d);
    }
    u32 sigma = isqrt_u32((u32)(var_acc / stat_n));
    if (sigma == 0)
      sigma = 1;

    report_trace_metrics(depth, mean, sigma);

    if (vga_bisect_should_dump(code)) {
      xil_printf("RAW,%d\r\n", depth);
      for (u32 i = 0; i < depth; i++)
        xil_printf("%d\r\n", g_trace[i]);
    }
  }

  xil_printf("\r\n  [INFO] restoring fine gain to the correct operating point\r\n");
  VgaDac_SetGainFine(AD8330_DEFAULT_GAIN_FINE_LINEAR);
  for (volatile u32 d = 0; d < DMA_POLL_ITERS_200MS; d++) {
  }
  calibrate_threshold();
}

#endif /* VGA_BISECT_ENABLE */

#define ENCODING_FOLD_DEMO_ENABLE 0 /* answered: see trigger_core_top.vhd. Flip to 1 to re-run. */
#define ANALOG_ZERO_CODE 8192
#if ENCODING_FOLD_DEMO_ENABLE

/* Reproduces the original bring-up artifact on demand, which is the one thing none of the earlier
 * hypotheses could do.
 *
 * The ADC's MODE pin is strapped to 2/3 VDD, so it emits 2's complement; trigger_core_top.vhd now
 * converts to offset binary with a single MSB flip before anything downstream sees it. Before that
 * conversion existed, the raw word was consumed as unsigned, which maps analog value V to
 * U = V for V >= 0 and V + 16384 for V < 0 -- monotonic EXCEPT across analog zero, where U folds
 * from 16383 straight to 0.
 *
 * The baseline sits ~6300 counts below analog zero (offset-binary ~1861; the same baseline read raw
 * is 1861 XOR 8192 = 10053, matching the "baseline close to 10000" in the early bring-up logs). So
 * pulses smaller than that gap never reach the fold and read perfectly, while larger ones cross it
 * and produce a discontinuity -- and if they also over-range the ADC they clip flat at the rail on
 * the far side. Rise, cliff, plateau, cliff back, exponential decay: the reported artifact, and
 * amplitude-dependent for a concrete reason.
 *
 * This drives the gain up until a pulse actually crosses analog zero, then prints that trace twice:
 * as corrected offset binary, and as the pre-fix firmware would have read it (offset ^ 0x2000).
 * The second column is the artifact. If the fold theory is right the two columns agree everywhere
 * below 8192 and diverge violently above it. */

static void test_encoding_fold_demo(void) {
  xil_printf("\r\n-- encoding fold demo: reproducing the original artifact --\r\n");

  /* Maximum gain on both channels, to get pulses tall enough to cross analog zero. Background
   * pulses reach only ~2400 counts at the normal operating point, well short of the ~6300 needed. */
  VgaDac_SetFineCodeRaw(1638);
  VgaDac_SetGainCoarse(21.0);
  for (volatile u32 d = 0; d < DMA_POLL_ITERS_200MS; d++) {
  }

  if (!calibrate_threshold()) {
    xil_printf("  [INFO] could not calibrate at max gain -- skipping demo\r\n");
    return;
  }

  /* Select only events that actually reach the fold, instead of dumping the first pulse to arrive. */
  u32 select = ANALOG_ZERO_CODE - 200;
  xil_printf("  [INFO] arming at %d to select events that cross analog zero (%d)...\r\n", select,
             ANALOG_ZERO_CODE);
  set_trigger_threshold(select);

  u32 count_before = g_raw_event_count;
  u32 waited;
  for (waited = 0; g_raw_event_count == count_before && waited < DMA_POLL_ITERS_10S; waited++) {
  }
  if (g_raw_event_count == count_before) {
    xil_printf("  [INFO] no event reached %d within ~10s -- gain still too low to reach the fold\r\n",
               select);
  } else {
    u32 depth = g_raw_ready_depth;
    read_raw_trace(g_raw_ready_buf, depth);

    u32 peak = 0, peak_idx = 0, crossings = 0;
    for (u32 i = 0; i < depth; i++) {
      if (g_trace[i] > peak) {
        peak = g_trace[i];
        peak_idx = i;
      }
      if (g_trace[i] >= ANALOG_ZERO_CODE)
        crossings++;
    }
    xil_printf("  [DATA] peak=%d @%d  samples at/above analog zero=%d\r\n", peak, peak_idx,
               crossings);

    /* Window around the peak: enough to show baseline, the fold and the recovery, without dumping
     * all 1024 samples twice. */
    u32 from = (peak_idx > 60) ? peak_idx - 60 : 0;
    u32 to = (peak_idx + 140 < depth) ? peak_idx + 140 : depth;
    xil_printf("FOLD,%d\r\n", to - from);
    xil_printf("idx,corrected,as_read_before_fix\r\n");
    for (u32 i = from; i < to; i++)
      xil_printf("%d,%d,%d\r\n", i, g_trace[i], g_trace[i] ^ ANALOG_ZERO_CODE);
  }

  xil_printf("  [INFO] restoring normal gain\r\n");
  VgaDac_SetGainFine(AD8330_DEFAULT_GAIN_FINE_LINEAR);
  VgaDac_SetGainCoarse(AD8330_DEFAULT_GAIN_COARSE_LINEAR);
  for (volatile u32 d = 0; d < DMA_POLL_ITERS_200MS; d++) {
  }
  calibrate_threshold();
}

#endif /* ENCODING_FOLD_DEMO_ENABLE */


/* Waits for the interrupt-driven result pipeline to publish an event, rather than owning axi_dma_0
 * itself. It used to arm axi_dma_0 for a single 8-byte one-shot and poll it -- which is what
 * deadlocked everything downstream of here.
 *
 * The mechanism: fci_core writes its result stream into axi_dma_0's S2MM. A one-shot arm satisfies
 * exactly one event, after which axi_dma_0 is unarmed and fci_core's output backpressures. Once
 * fci_core's small output FIFO fills it stops accepting input, and because axis_broadcaster_0 is
 * LOCKSTEP -- no beat advances unless BOTH consumers take it -- a stalled fci_core also freezes the
 * raw-trace tap on axi_dma_1. That is why raw_events would climb to ~5 (the FIFO's worth of events)
 * and then sit frozen for the rest of the run, with axi_dma_1's DMASR reading a perfectly healthy
 * "armed and waiting": it genuinely was armed, and no data was ever going to reach it.
 *
 * Both consumers are now serviced continuously by start_result_pipeline()/start_raw_trace_pipeline()
 * from before the first trigger, so neither can starve the other. */
static void test_live_event(void) {
  xil_printf("-- live event: trigger_core -> fci_core -> BRAM (interrupt-driven) --\r\n");

  xil_printf("  [INFO] waiting for a live trigger (up to ~10s)...\r\n");
  u32 count_before = g_event_count;
  u32 waited;
  for (waited = 0; g_event_count == count_before && waited < DMA_POLL_ITERS_10S; waited++) {
  }
  if (g_event_count == count_before) {
    xil_printf("  [FAIL] timed out waiting for a triggered event (check threshold/detector)\r\n");
    report_raw_path_state();
    g_fail_count++;
    return;
  }

  u32 raw_l = g_last_psa_l;
  u32 raw_w = g_last_psa_w;

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

/* Arms axi_dma_0's pipeline and hands it over to service_dma0_event() (defined earlier alongside
 * axi_dma_1's pipeline) -- from here on main()'s loop is free to do anything else (print, service
 * UART, etc.) without blocking acquisition; this is the real-time property the original
 * block-design audit called for ("even if the MicroBlaze is attending UART character parsing or
 * any other slow tasks"). Uses Intc_EnableAdditional(), not a second Intc_Init(), since
 * start_raw_trace_pipeline() already brought interrupts up for axi_dma_1 earlier -- re-running
 * Intc_Init() here would overwrite IER and silently disable that. */
/* Brings axi_dma_0 (fci_core's PSA result stream) up interrupt-driven and double-buffered, right
 * alongside the raw-trace pipeline and BEFORE any trigger can fire. Both axis_broadcaster_0
 * consumers must be continuously serviced from the start: the broadcaster is lockstep, so whichever
 * one stops accepting stalls the other as well. See test_live_event() for the failure this
 * prevents. Requires Intc_Init() to have run already (start_raw_trace_pipeline does it). */
#if !FCI_RESULT_VIA_FCI_SINK
static void start_result_pipeline(void) {
  if (!Dma_ResetCore(AXI_DMA_BASEADDR)) {
    xil_printf("  [FAIL] Dma_ResetCore (axi_dma_0) timed out\r\n");
    g_fail_count++;
    return;
  }

  g_write_buf = RESULT_BUF_A;
  DmaS2mm_ArmTransfer(AXI_DMA_BASEADDR, g_write_buf, 8);

  Intc_EnableAdditional(INTC_DMA_S2MM_BIT);
}
#endif /* !FCI_RESULT_VIA_FCI_SINK */

/* Both DMA channels have been running interrupt-driven since start_result_pipeline(); all this does
 * is restore the operating threshold that the bisect perturbed and hand over to the print loop. */
static void start_continuous_capture(void) {
  xil_printf("-- continuous interrupt-driven capture --\r\n");

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, 1024);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, TRIGGER_DELAY);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
  set_trigger_threshold(g_calibrated_threshold); /* see calibrate_threshold() */

  xil_printf("  [INFO] armed -- events now serviced by interrupt in the background\r\n");
}

void Bringup_Run(void) {
  xil_printf("\r\n=== FCI register bring-up test ===\r\n");
  test_trigger_core();
  test_fci_core();
  test_blr_core();
  test_psd_core();
#if FCI_RESULT_VIA_FCI_SINK
  test_fci_sink();
#endif
  test_vga_dac();

  start_fci_core_realtime();
  start_raw_trace_pipeline(); /* must be armed+interrupt-driven before any real trigger can occur
                                * -- see its comment for why */
#if !FCI_RESULT_VIA_FCI_SINK
  start_result_pipeline();    /* same requirement for the other broadcaster consumer: a one-shot
                                * axi_dma_0 here used to stall fci_core and, through the lockstep
                                * broadcaster, the raw-trace tap with it */
#else
  /* fci_sink needs no arming: it is always ready, drains into its own FIFO, and cannot stall
   * fci_core the way an unarmed axi_dma_0 could. That failure mode is designed out rather than
   * scheduled around. */
#endif
  calibrate_threshold();      /* measures the live baseline and derives the threshold from it;
                                * everything below depends on it, so it runs first */
  test_live_event();          /* "prove it end-to-end" on the running interrupt pipeline */
  test_raw_trace_capture();   /* reads back whatever the background pipeline above has captured
                                * by now, rather than arming/waiting itself */

  xil_printf("=== %s (%d failure%s) ===\r\n", g_fail_count == 0 ? "TEST PASSED" : "TEST FAILED",
             g_fail_count, g_fail_count == 1 ? "" : "s");

#if VGA_BISECT_ENABLE
  test_vga_fine_bisect(); /* restores the correct gain and recalibrates before returning */
#endif
#if ENCODING_FOLD_DEMO_ENABLE
  test_encoding_fold_demo(); /* likewise restores gain + recalibrates */
#endif

  start_continuous_capture();

#if FCI_RESULT_VIA_FCI_SINK
  /* Both result FIFOs are drained here by polling rather than from an ISR. At 30 cps that is
   * obviously sufficient, and at the 15 kcps design target a 32-deep FIFO still gives 2.1 ms of
   * slack per drain pass -- far more than this loop's period. The watermark interrupts are
   * configured by Acq_Configure() and left available for a future ISR-driven build; nothing here
   * depends on them. */
  {
    AcqStats stats;
    AcqEvent ev;
    u32 printed = 0;

    Acq_Configure(TRIGGER_DELAY, g_last_sigma);
    Acq_ResetStats(&stats);
    Acq_PrintCsvHeader();

    while (1) {
      if (Acq_PopPaired(&ev, &stats)) {
        Acq_PrintEventCsv(&ev);
        printed++;

        /* Same reasoning as the raw-trace dump below: a handful of early traces for a visual
         * sanity check against the reference pulse shape. */
        if (printed <= 3)
          print_raw_trace(g_raw_ready_buf, g_raw_ready_depth);

        /* Periodic health line. A nonzero dropped/overflow count is the signal that the two
         * result streams are slipping and the FCI-vs-PSD comparison is being resynchronized
         * rather than silently mispaired. */
        if ((printed % 100u) == 0u)
          Acq_PrintStats(&stats);
      }
    }
  }
#else
  /* psd_core has been integrating since power-on with nothing draining it, so its FIFO is long
   * since full and its overflow flag set. Clear it here so that what follows starts from a known
   * empty state and the positional pairing below is anchored.
   *
   * Positional pairing, not timestamp pairing: the HLS fci_core does not forward TUSER, so FCI
   * results carry no timestamp on this build. It is still sound, because axis_broadcaster_0 is
   * lockstep and psd_core emits exactly one result per frame -- the Nth PSD result is by
   * construction the Nth FCI result. What makes it fragile rather than structural is that a single
   * dropped result on either side shifts the alignment permanently and silently, so the overflow
   * flag is checked every event and reported loudly. Timestamp pairing (acquisition.c) replaces
   * this once fci_core_rtl forwards the tag. */
  /* Configure both new cores explicitly rather than leaning on their reset defaults. The defaults
   * happen to be right today -- psd_core resets to pre_trigger=100, which matches TRIGGER_DELAY --
   * but that is a coincidence between two constants in different files, and it would break
   * silently the moment TRIGGER_DELAY moved. psd_core has no other way to learn where the trigger
   * sits inside the frame.
   *
   * Gate geometry, in samples at 50 Msps, from the measured pulse (rise ~21 samples, decay
   * tau ~1.4 us ~ 70 samples): 32 pre-trigger samples inside the gate so a residual pedestal shows
   * up as a nonzero integral instead of hiding in the energy; 80 (~1.6 us, one decay constant) for
   * the prompt component; 400 (~8 us, ~5.7 decay constants) for essentially the full charge.
   * These are the discrimination knobs and are meant to be swept -- starting points matched to the
   * pulse, not derived optima. */
  /* Long gate cut from 400 to 250 after the first hardware run: at 400 the window reached into
   * the AFE's post-pulse undershoot and 14% of events integrated a NEGATIVE tail, reporting
   * El < Es. 250 ends at sample 318, comfortably before the undershoot seen in the captured
   * traces. This is a provisional value -- read the [SCAN] output above and set it to the length
   * at which cumulative charge peaks. */
  Psd_Configure(PSD_CORE_BASEADDR, TRIGGER_DELAY, PSD_PRE_GATE_SAMPLES, 80, 250, 0);

  /* blr_core's gate threshold derived from the sigma calibration just measured, rather than left
   * at the reset default: the gate has to stay open on noise and shut on pulses, and the only
   * honest source for "how much noise" is the same measurement the trigger threshold uses. */
  if (g_last_sigma > 0)
    Blr_Configure(BLR_CORE_BASEADDR, BLR_DEFAULT_SHIFT,
                  Blr_GateThresholdForSigma(g_last_sigma), BLR_DEFAULT_HOLDOFF);

  Psd_Clear(PSD_CORE_BASEADDR);

  /* CSV header. Everything below this line is either a data row or a '#'-prefixed comment, so the
   * capture can be pasted straight into a spreadsheet and the comment rows filtered or deleted in
   * one pass. */
  xil_printf("El,FCI,PSD\r\n");

  u32 last_printed_count = 0;
  u32 psd_desync_reported = 0;
  u32 skipped_total = 0;
  while (1) {
    u32 count = g_event_count; /* single volatile read, stable snapshot for this iteration */
    if (count != last_printed_count) {
      u32 psa_l = g_last_psa_l;
      u32 psa_w = g_last_psa_w;
      /* How many events actually happened since the last print. This is NOT always 1: the UART
       * takes ~3 ms per line at 115200, so at 30 cps two events can land between polls. The FCI
       * side only ever exposes the LATEST result (g_last_psa_*), so the printed line is about the
       * latest event -- which means the PSD side must be advanced by the same amount and the
       * latest of those used, or the two streams drift apart by one event per skip, permanently
       * and silently. An earlier version of this loop popped exactly one PSD result per print and
       * did precisely that. */
      u32 advanced = count - last_printed_count;
      last_printed_count = count;
      if (advanced > 1)
        skipped_total += advanced - 1;

      /* CSV row: El,FCI,PSD -- see the header printed above the loop. Fields are emitted in that
       * order regardless of which are available, so every row has exactly two commas and the
       * columns stay aligned even when a value is missing. A missing value is left EMPTY rather
       * than filled with a sentinel like 0 or -1, which a spreadsheet would happily average. */
      s32 fci_scaled = 0;
      int have_fci = (psa_w != 0);
      if (have_fci)
        fci_scaled = (s32)(((u64)psa_l * 10000ULL) / psa_w);

      /* PSD side of the same event: discard the results belonging to any events whose FCI value
       * was overwritten before it could be printed, and keep the last, which is the one the
       * printed FCI belongs to. */
      PsdResult pr;
      int have_psd = 0;
      for (u32 k = 0; k < advanced; k++)
        have_psd = Psd_Pop(PSD_CORE_BASEADDR, &pr);

      /* Column 1: El */
      if (have_psd)
        xil_printf("%d", pr.energy_long);
      xil_printf(",");

      /* Column 2: FCI */
      if (have_fci)
        print_fixed4(fci_scaled);
      xil_printf(",");

      /* Column 3: PSD = (El - Es) / El, CAEN's tail fraction. A non-positive El means the event
       * carried no net charge over the long gate -- noise, or the gate reaching into the AFE
       * undershoot -- and the ratio would be meaningless, so the field is left empty. */
      if (have_psd && pr.energy_long > 0) {
        s64 num = ((s64)pr.energy_long - (s64)pr.energy_short) * 10000LL;
        print_fixed4((s32)(num / (s64)pr.energy_long));
      }

      /* A PSD overflow means results were dropped, so every pairing after it is off by however
       * many were lost. Report once rather than every event, and keep going -- the FCI numbers
       * stay valid, only the pairing is suspect. */
      if (!psd_desync_reported && Psd_Overflowed(PSD_CORE_BASEADDR)) {
        psd_desync_reported = 1;
        xil_printf("\r\n# [WARN] psd FIFO overflowed -- PSD/FCI pairing is no longer aligned");
      }
      xil_printf("\r\n");

      /* Report the skip count periodically: a nonzero value is not an error -- it just means the
       * UART could not keep up and those events were dropped from BOTH streams together, which is
       * what keeps the pairing valid. It is worth seeing, because it is also the live rate at
       * which events are being lost to printing. */
      if ((count % 200u) == 0u && skipped_total > 0u)
        xil_printf("# %d event(s) skipped by the print loop so far (pairing still aligned)\r\n",
                   skipped_total);

      /* Dump the raw trace behind the first few real events too, for a quick visual sanity check
       * against the reference dataset's pulse shape without a separate bring-up run. axi_dma_1's
       * raw capture (1024 samples) finishes streaming well before fci_core's ~3249-cycle latency
       * produces the matching PSA result that triggers this print, so g_raw_ready_buf/_depth are
       * already this same event's trace by the time count advances here -- true as long as events
       * aren't arriving faster than that gap, which holds at the low background rates seen so far. */
      if (count <= 3)
        print_raw_trace(g_raw_ready_buf, g_raw_ready_depth);
    }
  }
#endif
  /* Unreachable: continuous capture runs until reset, no cleanup_platform()/return path. */
}
