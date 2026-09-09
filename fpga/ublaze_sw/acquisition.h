/**
 * @file acquisition.h
 * @brief Pairs psd_core and fci_sink results that came from the same pulse, and configures the
 *        spectroscopy chain (blr_core -> trigger_core -> {fci_core, psd_core}).
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
#include "pulse_shaper.h"
#include "xil_types.h"

/** @brief One pulse's paired FCI + PSD result -- this project's list-mode event record. */
typedef struct {
  u64 timestamp;    /**< trigger_core's 64-bit cycle count at the moment this pulse fired. */
  /* FCI side */
  u32 psa_l;        /**< FCI numerator window accumulator. */
  u32 psa_w;        /**< FCI denominator window accumulator. */
  u32 fci_scaled;   /**< PSA_l/PSA_w * 10000. */
  /* PSD side */
  s32 energy_short; /**< PSD short-gate charge integral. */
  s32 energy_long;  /**< PSD long-gate charge integral. */
  s32 psd_scaled;   /**< (long-short)/long * 10000, the CAEN PSD parameter. */
  s32 peak;         /**< Shaped-pulse plateau amplitude from pulse_shaper_core (a Jordanov-Knoll
                      *   recursive trapezoidal filter) -- the spectroscopy energy channel,
                      *   independent of the PSD gates. Field name/position/type are unchanged
                      *   from the raw single-sample peak this replaced (psd_core's former
                      *   PSD_PEAK_OFFSET), so nothing downstream of AcqEvent -- the CLI wire
                      *   format, fci_api, the GUI -- needed to change; only the hardware source
                      *   did. Reads 0 if this firmware was built against a bitstream that predates
                      *   pulse_shaper_core (see PULSE_SHAPER_CORE_PRESENT in registers.h): there is
                      *   no fallback amplitude source in that configuration any more, since the
                      *   raw-peak logic this replaced was removed from psd_core at the same time. */
} AcqEvent;

/** @brief Running health counters, so a desync shows up as a number rather than as puzzling data. */
typedef struct {
  u32 paired;             /**< Events successfully matched on all sides. */
  u32 dropped_psd;        /**< PSD results discarded while resynchronizing. */
  u32 dropped_fci;        /**< FCI results discarded while resynchronizing. */
  u32 dropped_shaper;     /**< Pulse-shaper results discarded while resynchronizing. */
  u32 psd_overflows;      /**< Times psd_core reported a full FIFO. */
  u32 fci_overflows;      /**< Times fci_sink reported a full FIFO. */
  u32 shaper_overflows;   /**< Times pulse_shaper_core reported a full FIFO. */
  u32 fci_framing_errors; /**< FCI result frames received out of sequence. */
} AcqStats;

/**
 * @brief Configures blr_core, trigger_core's gate-related registers, psd_core and fci_sink into a
 *        consistent set.
 * @param trigger_delay trigger_core's delay register value, also passed to psd_core as pre_trigger.
 * @param sigma Measured baseline noise, used to derive the BLR gate threshold; pass 0 to use the
 *              reset default.
 */
void Acq_Configure(u32 trigger_delay, u32 sigma);

/**
 * @brief Pops one matched event.
 *
 * Resynchronizes by discarding whichever side is older when timestamps disagree.
 *
 * @param out   Filled in on success; untouched otherwise.
 * @param stats Updated with pairing/overflow/framing counters regardless of outcome.
 * @return 1 if an event was produced, 0 if either FIFO ran dry.
 */
int Acq_PopPaired(AcqEvent *out, AcqStats *stats);

/** @brief Zeroes all counters in @p stats. */
void Acq_ResetStats(AcqStats *stats);
/** @brief Prints @p stats in human-readable form over the CLI's reply channel. */
void Acq_PrintStats(const AcqStats *stats);
/** @brief Prints one event in human-readable form over the CLI's reply channel. */
void Acq_PrintEvent(const AcqEvent *ev);

/** @brief CSV header matching Acq_PrintEventCsv()'s row format, for host-side capture. */
void Acq_PrintCsvHeader(void);
/** @brief Prints one event as a CSV row matching Acq_PrintCsvHeader()'s column order. */
void Acq_PrintEventCsv(const AcqEvent *ev);

#endif /* SRC_ACQUISITION_H_ */
