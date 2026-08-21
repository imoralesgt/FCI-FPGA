/*
 * acquisition.h
 *
 * Pairs psd_core and fci_sink results that came from the same pulse, and configures the
 * spectroscopy chain (blr_core -> trigger_core -> {fci_core, psd_core}).
 *
 * Why pairing needs doing at all: both cores see every event, in the same order, because
 * axis_broadcaster_0 is lockstep. But each buffers its results in its own FIFO, and the two are
 * drained by separate register reads. If either overflows -- or if firmware ever drains one more
 * eagerly than the other -- the two streams slip relative to each other, and from then on every
 * "FCI vs PSD" comparison would be pairing one pulse's FCI with a different pulse's PSD. The
 * 64-bit timestamp trigger_core stamps on TUSER is what makes that detectable and recoverable
 * rather than silently wrong.
 */

#ifndef SRC_ACQUISITION_H_
#define SRC_ACQUISITION_H_

#include "fci_sink.h"
#include "psd.h"
#include "xil_types.h"

typedef struct {
  u64 timestamp;
  /* FCI side */
  u32 psa_l;
  u32 psa_w;
  u32 fci_scaled; /* PSA_l/PSA_w * 10000 */
  /* PSD side */
  s32 energy_short;
  s32 energy_long;
  s32 psd_scaled; /* (long-short)/long * 10000, the CAEN PSD parameter */
} AcqEvent;

/* Running health counters, so a desync shows up as a number rather than as puzzling data. */
typedef struct {
  u32 paired;         /* events successfully matched on both sides */
  u32 dropped_psd;    /* PSD results discarded while resynchronizing */
  u32 dropped_fci;    /* FCI results discarded while resynchronizing */
  u32 psd_overflows;  /* times psd_core reported a full FIFO */
  u32 fci_overflows;
  u32 fci_framing_errors;
} AcqStats;

/* Configures blr_core, trigger_core's gate-related registers, psd_core and fci_sink into a
 * consistent set. sigma is the measured baseline noise, used to derive the BLR gate threshold;
 * pass 0 to use the reset default. */
void Acq_Configure(u32 trigger_delay, u32 sigma);

/* Pops one matched event. Returns 1 if an event was produced, 0 if either FIFO ran dry.
 * Resynchronizes by discarding whichever side is older when timestamps disagree. */
int Acq_PopPaired(AcqEvent *out, AcqStats *stats);

void Acq_ResetStats(AcqStats *stats);
void Acq_PrintStats(const AcqStats *stats);
void Acq_PrintEvent(const AcqEvent *ev);

/* CSV header matching Acq_PrintEvent's row format, for host-side capture. */
void Acq_PrintCsvHeader(void);
void Acq_PrintEventCsv(const AcqEvent *ev);

#endif /* SRC_ACQUISITION_H_ */
