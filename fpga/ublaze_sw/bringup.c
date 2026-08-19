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
#include "registers.h"
#include "vga_dac.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "xstatus.h"

/* trigger_core's capture depth. NOT a tunable: it must equal fci_core's N_SAMPLES exactly.
 * trigger_core streams exactly this many beats per capture and fci_core's axis_to_fft loop reads
 * exactly that many, so any mismatch either hangs the pipeline (fci_core waiting on beats that
 * never arrive) or desyncs every frame after it. Named rather than written inline at each of the
 * four sites that program it, because a literal 1024 reads like a tunable and this is a hard
 * interface constraint between two cores. */
#define CAPTURE_DEPTH 1024

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

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, CAPTURE_DEPTH);
  check_u32("depth", CAPTURE_DEPTH, Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET));

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
/* Waits that must catch a REAL detector event. Background here is ~30 cps, so P(no event in 1 s)
 * = exp(-30) ~= 1e-13 -- 2 s is already far past the point where waiting longer buys anything. An
 * earlier version used 10 s, sized from an event-rate estimate that was 10x low; that only made
 * every failing run slower to report. The timeout is set so that EXPIRY IS ITSELF INFORMATIVE:
 * at this rate it means the pipeline is broken, never that the detector was quiet. */
#define DMA_POLL_ITERS_EVENT DMA_POLL_ITERS_2S

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
/* Bytes the S2MM channel is armed for. Deliberately the FULL buffer capacity, NOT the expected
 * packet length (depth * 2).
 *
 * Arming for exactly the expected length makes the channel fragile in a way that costs the whole
 * acquisition chain: if a packet ever runs longer than the programmed length -- a boundary slip, a
 * re-arm landing mid-packet, an RTL bug -- the DataMover raises DMAIntErr and HALTS. A halted S2MM
 * never re-arms itself, and because axis_broadcaster_0 is lockstep it also freezes fci_core and
 * trigger_core, so one bad packet reads as "the whole design is dead" (observed 2026-08-18:
 * raw_events stuck at 1, S2MM_DMASR=0x11).
 *
 * Arming for capacity cannot overrun for any packet up to RAW_TRACE_MAX_SAMPLES beats, and TLAST
 * still terminates the transfer at the true packet boundary. The byte count actually received is
 * then read back from the length register, which is strictly better information than assuming the
 * depth register still holds what it held when the trigger fired.
 *
 * HARDWARE COUPLING: axi_dma_1's c_sg_length_width (the buffer-length register width) must be at
 * least ceil(log2(RAW_TRACE_ARM_BYTES + 1)) = 13 bits for this value to survive the write. The
 * register silently truncates, and a truncated 4096 is 0 -- which the DataMover reports as
 * DMAIntErr, the same error code as a packet overrun, from a completely different cause. Not
 * hypothetical: axi_dma_0 shipped at 8 bits (max 255 bytes) until 2026-08-18. Both DMAs are now
 * at 16, which leaves margin; if this constant ever grows past 65535, that width must grow too. */
#define RAW_TRACE_ARM_BYTES (CAPTURE_DEPTH * 2)
/* NOTE (2026-08-18): this is deliberately back to the EXACT expected packet length, not the buffer
 * capacity above. Capacity arming is the more robust pattern and the reasoning above still holds --
 * but it relies on TLAST to terminate the transfer, and on this hardware TLAST is currently not
 * ending the S2MM transfer: the channel swallowed two 1024-beat packets and raised DMAIntErr at
 * 4096 bytes. Exact-length arming completes at the byte count instead, which is the configuration
 * that ran 10,000+ events without a single error.
 *
 * The cost of this choice is that a TLAST fault stays invisible rather than loud, so the `last rx=`
 * figure in report_raw_path_state() is the thing to watch: 2048 bytes means the packet ended where
 * it should. Revert to RAW_TRACE_MAX_SAMPLES * 2 once the TLAST path into axi_dma_1 is understood. */
static volatile u32 g_raw_last_len = 0; /* bytes in the most recent completed transfer */
static volatile u32 g_fsl_timeouts = 0; /* MM2S readbacks that never delivered; see the ISR */
/* ~240 us at 50 MHz. An 8-byte MM2S transfer completes in well under a microsecond, so this is
 * pure margin -- its only job is to be finite, because the alternative is an unbounded stall. */
#define MM2S_WAIT_ITERS 2000

/* Counts how often the raw-trace channel had to be reset. A nonzero value here with acquisition
 * still running means errors are occurring but no longer fatal -- which is exactly the information
 * needed to tell "one unlucky packet" apart from "every packet is malformed". */
static volatile u32 g_dma1_recoveries = 0;
/* Consecutive recoveries with no clean capture in between. Recovery is worth having for an
 * occasional bad packet, but unbounded it is actively harmful: when the error repeats immediately
 * the ISR resets and re-arms forever (measured 378k times in one run) and NOTHING else can be
 * observed -- no captures, no calibration, and the original fault buried under firmware thrashing.
 * After this many consecutive failures the channel is left halted on purpose, which restores the
 * previous, more informative behaviour: one error, then a stable state the diagnostics can read. */
#define DMA1_MAX_CONSEC_RECOVERIES 8
static volatile u32 g_dma1_consec_fail = 0;
static volatile u32 g_dma1_gave_up = 0;

static void service_dma1_event(void) {
  /* Snapshot status and byte count BEFORE the ack clears the completion state. The S2MM length
   * register reads back what was actually received, which is the truth; the trigger_core depth
   * register only says what SHOULD have arrived. */
  u32 sr = Xil_In32(AXI_DMA_1_BASEADDR + AXI_DMA_S2MM_DMASR_OFFSET);
  u32 rx_bytes = Xil_In32(AXI_DMA_1_BASEADDR + AXI_DMA_S2MM_LENGTH_OFFSET);

  DmaS2mm_AckComplete(AXI_DMA_1_BASEADDR);

  int errored = (sr & AXI_DMA_SR_ERR_ALL_MASK) != 0;
  int completed = (sr & AXI_DMA_SR_IOC_IRQ_MASK) != 0;

  u32 just_completed = g_raw_write_buf;

  /* Re-arm into the other slot before publishing/reading this one, same reasoning as the PSA
   * double-buffer below. */
  g_raw_write_buf = (g_raw_write_buf == RAW_TRACE_BUF_A) ? RAW_TRACE_BUF_B : RAW_TRACE_BUF_A;
  if (errored) {
    if (g_dma1_consec_fail >= DMA1_MAX_CONSEC_RECOVERIES) {
      g_dma1_gave_up = 1; /* leave it halted; stop the spin so the state stays readable */
      return;
    }
    g_dma1_consec_fail++;
    g_dma1_recoveries++;
    DmaS2mm_RecoverIfHalted(AXI_DMA_1_BASEADDR); /* reset clears the halt */
  }
  DmaS2mm_ArmTransfer(AXI_DMA_1_BASEADDR, g_raw_write_buf, RAW_TRACE_ARM_BYTES);

  /* Publish ONLY a clean, non-empty completion. Counting error recoveries as events made
   * g_raw_event_count advance on entries where no data arrived at all, which fed straight back
   * into find_noise_band(): every scan step saw the counter move and reported a "noise band"
   * spanning 0..16376 -- including thresholds that cannot physically fire. The scan was measuring
   * the recovery loop rather than the detector. */
  if (completed && !errored && rx_bytes > 0) {
    g_dma1_consec_fail = 0; /* a good transfer re-earns the recovery budget */
    g_raw_ready_buf = just_completed;
    g_raw_ready_depth = rx_bytes / 2; /* 16-bit stream: 2 bytes per sample */
    g_raw_last_len = rx_bytes;
    g_raw_event_count++;
  }
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
static void set_trigger_threshold(u32 threshold) {
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, 16383);
  for (volatile u32 i = 0; i < 1000; i++) {
  }
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_THRESHOLD_OFFSET, threshold);
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
  /* Never issue the blocking FSL read without first confirming the words are coming. getfslx() with
   * FSL_DEFAULT stalls the core until a word arrives, and this runs inside the ISR -- so a failed
   * readback wedges the firmware permanently with interrupts disabled and no output at all. That is
   * what turned a DMA error into a silent stop mid-line. */
  if (!DmaMm2s_WaitDone(AXI_DMA_BASEADDR, MM2S_WAIT_ITERS)) {
    g_fsl_timeouts++;
    return;
  }
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

  g_raw_write_buf = RAW_TRACE_BUF_A;
  DmaS2mm_ArmTransfer(AXI_DMA_1_BASEADDR, g_raw_write_buf, RAW_TRACE_ARM_BYTES);

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
static u16 g_trace[RAW_TRACE_MAX_SAMPLES];

/* Streams `depth` samples out of the raw-trace BRAM at buf_addr into g_trace. Kept separate from
 * printing so calibrate_threshold() can analyze a trace without dumping it over the UART. */
static int read_raw_trace(u32 buf_addr, u32 depth) {
  DmaMm2s_ArmTransfer(AXI_DMA_1_BASEADDR, buf_addr, depth * 2);
  if (!DmaMm2s_WaitDone(AXI_DMA_1_BASEADDR, MM2S_WAIT_ITERS)) {
    g_fsl_timeouts++; /* see service_dma0_event(): do NOT fall through to a blocking read */
    return 0;
  }
  u32 copied = 0;
  while (copied < depth) {
    u32 word;
    getfslx(word, 1, FSL_DEFAULT);
    g_trace[copied++] = (u16)(word & 0xFFFF);
    if (copied < depth)
      g_trace[copied++] = (u16)((word >> 16) & 0xFFFF);
  }
  return 1;
}

static void print_raw_trace(u32 buf_addr, u32 depth) {
  if (!read_raw_trace(buf_addr, depth)) {
    xil_printf("  [FAIL] MM2S readback timed out -- no trace to print\r\n");
    return;
  }

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
/* Names the S2MM status bits instead of leaving a hex word to be looked up in PG021. The error
 * bits matter most: DMAIntErr halts the channel permanently, and in direct-register mode it means
 * either a zero transfer length or -- far more likely here -- a packet LARGER than the programmed
 * buffer, i.e. the stream ran past `depth` beats without TLAST. A halted channel then stalls the
 * lockstep broadcaster and takes the whole acquisition chain down with it, which is why a single
 * error shows up as "nothing works" rather than as one lost trace. */
static void print_dmasr(const char *label, u32 sr) {
  xil_printf("  [DIAG] %s DMASR=0x%08x%s%s%s%s%s%s\r\n", label, sr,
             (sr & 0x00000001u) ? " HALTED" : "",
             (sr & 0x00000002u) ? " IDLE" : "",
             (sr & 0x00000010u) ? " DMAIntErr(len=0 or packet>buffer)" : "",
             (sr & 0x00000020u) ? " DMASlvErr" : "",
             (sr & 0x00000040u) ? " DMADecErr" : "",
             (sr & 0x00001000u) ? " IOC" : "");
}

static void report_raw_path_state(void) {
  print_dmasr("dma1 S2MM", Xil_In32(AXI_DMA_1_BASEADDR + AXI_DMA_S2MM_DMASR_OFFSET));
  print_dmasr("dma1 MM2S", Xil_In32(AXI_DMA_1_BASEADDR + AXI_DMA_MM2S_DMASR_OFFSET));
  xil_printf("  [DIAG] raw_events=%d  last rx=%d bytes (%d samples)  trigger_core depth=%d\r\n",
             g_raw_event_count, g_raw_last_len, g_raw_last_len / 2,
             Xil_In32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET));
  xil_printf("  [DIAG] fsl timeouts=%d  dma1 recoveries=%d (consec=%d%s)  clean captures=%d\r\n",
             g_fsl_timeouts, g_dma1_recoveries, g_dma1_consec_fail,
             g_dma1_gave_up ? ", GAVE UP" : "", g_raw_event_count);
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
#define BASELINE_SAMPLES 64 /* comfortably inside the TRIGGER_DELAY pre-trigger region */
#define THRESHOLD_SIGMA_MULT 8

static u32 g_calibrated_threshold; /* 0 until calibrate_threshold() succeeds */

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
  /* ~300 us at 50 MHz: comfortably longer than the ~85 us a capture needs to retire. */
#define SETTLE_ITERS (DMA_POLL_ITERS_200MS / 200)

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
    /* Let any capture already in flight under the previous threshold retire before snapshotting.
     * A capture is 1024 beats plus fci_core's 3197-cycle interval, ~4200 cycles ~= 85 us, so a few
     * hundred microseconds is ample. This used to be 20000 iterations (~2.4 ms), four times the
     * dwell it protects and ~80% of the whole scan's cost, for no benefit. */
    for (volatile u32 q = 0; q < SETTLE_ITERS; q++) {
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

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, CAPTURE_DEPTH);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, TRIGGER_DELAY);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);

  /* A halted or errored S2MM cannot complete ANY capture, so every one of the scan's 2048 steps
   * would dwell and fail -- seconds of silence ending in "no noise band found", which describes a
   * symptom and not the cause. Check once, up front, and name the real fault immediately. */
  if (!g_dma1_gave_up && DmaS2mm_RecoverIfHalted(AXI_DMA_1_BASEADDR)) {
    g_dma1_recoveries++;
    xil_printf("  [WARN] axi_dma_1 S2MM was halted on an error -- reset and re-armed\r\n");
    g_raw_write_buf = RAW_TRACE_BUF_A;
    DmaS2mm_ArmTransfer(AXI_DMA_1_BASEADDR, g_raw_write_buf, RAW_TRACE_ARM_BYTES);
  }

  u32 band_lo, band_hi;
  if (!find_noise_band(&band_lo, &band_hi)) {
    xil_printf("  [FAIL] no noise band found -- cannot calibrate\r\n");
    report_raw_path_state();
    g_fail_count++;
    return 0;
  }
  /* Band center is the baseline, to within the scan step -- a useful cross-check against the mean
   * computed from the trace below, since the two are measured completely differently. */
  u32 band_mid = (band_lo + band_hi) / 2;
  xil_printf("  [INFO] noise band spans thresholds %d..%d (center %d)\r\n", band_lo, band_hi,
             band_mid);

  /* Park at the CENTER of the band, not its top edge. The edges are by definition where crossings
   * are rarest, so parking there can find the band and then wait out the timeout without a single
   * capture; the center crosses at kHz and returns immediately. */
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
  if (!read_raw_trace(g_raw_ready_buf, g_raw_ready_depth)) {
    xil_printf("  [FAIL] MM2S readback timed out during calibration\r\n");
    g_fail_count++;
    return 0;
  }

  u32 sum = 0;
  for (u32 i = 0; i < depth; i++)
    sum += g_trace[i];
  u32 mean = sum / depth;

  /* 64-bit accumulator: if the "pre-trigger" window is not actually quiet (a pulse landing early,
   * or a capture triggered mid-event), a single outlier contributes d*d up to ~2.7e8 and a u32
   * would wrap, producing a silently wrong sigma and thus a nonsense threshold. */
  u64 var_acc = 0;
  for (u32 i = 0; i < depth; i++) {
    int d = (int)g_trace[i] - (int)mean;
    var_acc += (u64)((s64)d * d);
  }
  u32 sigma = isqrt_u32((u32)(var_acc / depth));
  if (sigma == 0) /* a perfectly flat window would otherwise collapse the margin to zero */
    sigma = 1;

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

static void report_trace_metrics(u32 depth, u32 mean, u32 sigma) {
  u32 peak = 0, peak_idx = 0;
  for (u32 i = 0; i < depth; i++) {
    if (g_trace[i] > peak) {
      peak = g_trace[i];
      peak_idx = i;
    }
  }

  u32 plateau_floor = (peak > 3 * sigma) ? peak - 3 * sigma : 0;
  u32 plateau = 0;
  for (u32 i = 0; i < depth; i++)
    if (g_trace[i] >= plateau_floor)
      plateau++;

  u32 trough = 0xFFFF;
  for (u32 i = peak_idx; i < depth; i++)
    if (g_trace[i] < trough)
      trough = g_trace[i];

  int amp = (int)peak - (int)mean;
  int undershoot = (int)mean - (int)trough;

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
    for (waited = 0; g_raw_event_count == count_before && waited < DMA_POLL_ITERS_EVENT; waited++) {
    }
    if (g_raw_event_count == count_before) {
      xil_printf("  [INFO] no event captured within ~2s at this gain\r\n");
      continue;
    }

    u32 depth = g_raw_ready_depth;
    if (!read_raw_trace(g_raw_ready_buf, depth))
      continue;

    u32 stat_n = (depth > BASELINE_SAMPLES) ? BASELINE_SAMPLES : depth;
    u32 sum = 0;
    for (u32 i = 0; i < stat_n; i++)
      sum += g_trace[i];
    u32 mean = sum / stat_n;
    u64 var_acc = 0;
    for (u32 i = 0; i < stat_n; i++) {
      int d = (int)g_trace[i] - (int)mean;
      var_acc += (u64)((s64)d * d);
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
  for (waited = 0; g_raw_event_count == count_before && waited < DMA_POLL_ITERS_EVENT; waited++) {
  }
  if (g_raw_event_count == count_before) {
    xil_printf("  [INFO] no event reached %d within ~2s -- gain still too low to reach the fold\r\n",
               select);
  } else {
    u32 depth = g_raw_ready_depth;
    if (!read_raw_trace(g_raw_ready_buf, depth)) {
      xil_printf("  [FAIL] MM2S readback timed out -- cannot render the fold\r\n");
      goto restore_gain;
    }

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

restore_gain:
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

  xil_printf("  [INFO] waiting for a live trigger (up to ~2s)...\r\n");
  u32 count_before = g_event_count;
  u32 waited;
  for (waited = 0; g_event_count == count_before && waited < DMA_POLL_ITERS_EVENT; waited++) {
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

/* Both DMA channels have been running interrupt-driven since start_result_pipeline(); all this does
 * is restore the operating threshold that the bisect perturbed and hand over to the print loop. */
static void start_continuous_capture(void) {
  xil_printf("-- continuous interrupt-driven capture --\r\n");

  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DEPTH_OFFSET, CAPTURE_DEPTH);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_DELAY_OFFSET, TRIGGER_DELAY);
  Xil_Out32(TRIGGER_CORE_BASEADDR + TRIGGER_CORE_POLARITY_OFFSET, TRIGGER_CORE_POLARITY_RISING);
  set_trigger_threshold(g_calibrated_threshold); /* see calibrate_threshold() */

  xil_printf("  [INFO] armed -- events now serviced by interrupt in the background\r\n");
}

void Bringup_Run(void) {
  xil_printf("\r\n=== FCI register bring-up test ===\r\n");
  test_trigger_core();
  test_fci_core();
  test_vga_dac();

  start_fci_core_realtime();
  start_raw_trace_pipeline(); /* must be armed+interrupt-driven before any real trigger can occur
                                * -- see its comment for why */
  start_result_pipeline();    /* same requirement for the other broadcaster consumer: a one-shot
                                * axi_dma_0 here used to stall fci_core and, through the lockstep
                                * broadcaster, the raw-trace tap with it */
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
  /* Unreachable: continuous capture runs until reset, no cleanup_platform()/return path. */
}
