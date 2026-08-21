/*
 * acquisition.c
 *
 * See acquisition.h.
 */

/* This file is inert until fci_sink is present in the block design. It stays in the project either
 * way, so that adding the core is a block-design change plus a BSP regeneration with no source
 * files to add or remove -- but referencing a base address the hardware does not export would
 * break the build for a BD that is otherwise perfectly valid. */
#include "xparameters.h"
#ifdef XPAR_FCI_SINK_0_BASEADDR

#include "acquisition.h"

#include "blr.h"
#include "registers.h"
#include "xil_io.h"
#include "xil_printf.h"

/* Gate geometry, in samples at 50 Msps, derived from this detector's measured pulse (rise ~21
 * samples / 420 ns, decay tau ~1.4 us ~ 70 samples -- see the project log, section 7):
 *
 *   PRE_GATE    32   ~0.6 us of pre-trigger baseline inside the gate. Its only job is to let a
 *                    nonzero pedestal show up as a nonzero integral, which is how a broken
 *                    baseline restoration announces itself instead of hiding in the energy.
 *   SHORT_GATE  80   ~1.6 us, a little over one decay constant: the prompt component.
 *   LONG_GATE  400   ~8 us, about 5.7 decay constants, so essentially the full charge.
 *
 * These are the discrimination knobs and are meant to be swept: the whole point of the exercise is
 * finding the pair that separates gammas from neutrons best on this detector. They are starting
 * points matched to the pulse, not derived optima.
 */
#define PSD_PRE_GATE 32
#define PSD_SHORT_GATE 80
#define PSD_LONG_GATE 400

/* Drain in batches rather than one interrupt per event: at the 15 kcps design target a per-event
 * ISR is 15,000 interrupts a second, where the entry/exit overhead alone starts to dominate. Both
 * FIFOs are 32 deep, so a watermark of 8 leaves 24 events of headroom for interrupt latency. */
#define ACQ_WATERMARK 8

void Acq_Configure(u32 trigger_delay, u32 sigma) {
  u32 gate_thr = (sigma > 0u) ? Blr_GateThresholdForSigma(sigma) : BLR_DEFAULT_GATE_THR;

  /* Hold-off must outlast the pulse or the gate reopens on the decaying tail and every event drags
   * the baseline upward -- measured at 718 counts of drift over six pulses before this existed.
   * 384 samples is 7.7 us, past 5 decay constants. */
  Blr_Configure(BLR_CORE_BASEADDR, BLR_DEFAULT_SHIFT, gate_thr, BLR_DEFAULT_HOLDOFF);

  /* pre_trigger MUST equal what trigger_core's delay register holds: it tells psd_core where the
   * trigger sits inside the frame, and there is no other way for it to know. Passing the same
   * value both places from one caller is what keeps them from drifting apart. */
  /* Reference 0: blr_core restores the baseline to zero, so charge integrates directly about
   * zero. This register survives as a residual-pedestal trim, not as a representation constant. */
  Psd_Configure(PSD_CORE_BASEADDR, trigger_delay, PSD_PRE_GATE, PSD_SHORT_GATE, PSD_LONG_GATE, 0);

  Psd_SetWatermark(PSD_CORE_BASEADDR, ACQ_WATERMARK);
  FciSink_SetWatermark(FCI_SINK_BASEADDR, ACQ_WATERMARK);

  /* Start both FIFOs empty and both sticky flags clear, so any overflow or framing error reported
   * afterwards belongs to this run. */
  Psd_Clear(PSD_CORE_BASEADDR);
  FciSink_Clear(FCI_SINK_BASEADDR);
}

void Acq_ResetStats(AcqStats *stats) {
  stats->paired = 0;
  stats->dropped_psd = 0;
  stats->dropped_fci = 0;
  stats->psd_overflows = 0;
  stats->fci_overflows = 0;
  stats->fci_framing_errors = 0;
}

/* CAEN's PSD parameter: (Long - Short) / Long, i.e. the fraction of the charge that arrives in the
 * tail. Scaled by 10000 for integer printing. Returns 0 when energy_long <= 0, which happens for
 * noise triggers whose integral is negative -- those carry no shape information and the caller
 * should treat a 0 here as "not meaningful" rather than as a real ratio. */
static s32 psd_parameter_scaled(s32 energy_short, s32 energy_long) {
  s64 num;
  if (energy_long <= 0)
    return 0;
  num = ((s64)energy_long - (s64)energy_short) * 10000LL;
  return (s32)(num / (s64)energy_long);
}

int Acq_PopPaired(AcqEvent *out, AcqStats *stats) {
  PsdResult p;
  FciResult f;

  if (!Psd_Peek(PSD_CORE_BASEADDR, &p))
    return 0;
  if (!FciSink_Peek(FCI_SINK_BASEADDR, &f))
    return 0;

  /* Resynchronize: whichever side is holding the older event has one the other side already lost,
   * so discard it and look again. Timestamps come from a single free-running counter, so "older"
   * is a plain comparison -- no wrap handling, since 64 bits at 50 MHz lasts ~11,700 years. */
  while (p.timestamp != f.timestamp) {
    if (p.timestamp < f.timestamp) {
      Psd_Discard(PSD_CORE_BASEADDR);
      stats->dropped_psd++;
      if (!Psd_Peek(PSD_CORE_BASEADDR, &p))
        return 0;
    } else {
      FciSink_Discard(FCI_SINK_BASEADDR);
      stats->dropped_fci++;
      if (!FciSink_Peek(FCI_SINK_BASEADDR, &f))
        return 0;
    }
  }

  Psd_Discard(PSD_CORE_BASEADDR);
  FciSink_Discard(FCI_SINK_BASEADDR);

  out->timestamp = p.timestamp;
  out->psa_l = f.psa_l;
  out->psa_w = f.psa_w;
  out->fci_scaled = FciSink_RatioScaled(&f);
  out->energy_short = p.energy_short;
  out->energy_long = p.energy_long;
  out->psd_scaled = psd_parameter_scaled(p.energy_short, p.energy_long);

  stats->paired++;

  /* Sticky flags are polled here rather than in their own pass: this is the one place that runs
   * once per event no matter how the caller structures its loop. */
  if (Psd_Overflowed(PSD_CORE_BASEADDR))
    stats->psd_overflows++;
  if (FciSink_Overflowed(FCI_SINK_BASEADDR))
    stats->fci_overflows++;
  if (FciSink_FramingError(FCI_SINK_BASEADDR))
    stats->fci_framing_errors++;

  return 1;
}

static void print_scaled(const char *label, s32 scaled) {
  s32 whole = scaled / 10000;
  s32 frac = scaled % 10000;
  if (frac < 0)
    frac = -frac;
  xil_printf("%s=%d.%04d", label, whole, frac);
}

void Acq_PrintEvent(const AcqEvent *ev) {
  xil_printf("  ts=%u:%08u  ", (u32)(ev->timestamp >> 32), (u32)ev->timestamp);
  print_scaled("FCI", (s32)ev->fci_scaled);
  xil_printf("  Es=%d El=%d  ", ev->energy_short, ev->energy_long);
  print_scaled("PSD", ev->psd_scaled);
  xil_printf("\r\n");
}

/* Machine-readable form for host-side capture: one row per event, so a PC-side tool can plot PSD
 * against FCI directly and see whether the two separate the same populations. That comparison is
 * the reason this whole path exists. */
void Acq_PrintCsvHeader(void) {
  xil_printf("EVT,ts_hi,ts_lo,psa_l,psa_w,fci_x10000,energy_short,energy_long,psd_x10000\r\n");
}

void Acq_PrintEventCsv(const AcqEvent *ev) {
  xil_printf("EVT,%u,%u,%u,%u,%u,%d,%d,%d\r\n", (u32)(ev->timestamp >> 32), (u32)ev->timestamp,
             ev->psa_l, ev->psa_w, ev->fci_scaled, ev->energy_short, ev->energy_long,
             ev->psd_scaled);
}

void Acq_PrintStats(const AcqStats *stats) {
  xil_printf("  [STATS] paired=%u  dropped(psd=%u fci=%u)  overflow(psd=%u fci=%u)  framing=%u\r\n",
             stats->paired, stats->dropped_psd, stats->dropped_fci, stats->psd_overflows,
             stats->fci_overflows, stats->fci_framing_errors);
}

#endif /* XPAR_FCI_SINK_0_BASEADDR */
