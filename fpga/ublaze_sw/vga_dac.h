/*
 * vga_dac.h
 *
 * AD5697 dual 12-bit DAC (I2C addr 0x0D) driving the AD8330 VGA's fine (channel A) and coarse
 * (channel B) gain-control voltages, per docs/fpga/DPP4SiPMCMOD_AFE.pdf. Uses the AD5697's
 * internal 2.5V reference, enabled by the same 0x70/0x00/0x00 command the sibling
 * gamma-spectroscopy project sends (NSIL-MCA-DPP4SiPM, WriteCommandToAD5697 case 7, built with
 * AD5697 defined and AD5697_EXT_REF undefined).
 *
 * The two channels use DIFFERENT control laws -- this is the key detail. In the sibling project
 * the conversion happens host-side in its Python API (sw/python-api/core/dpp_parameters.py); its
 * MicroBlaze only ever receives the finished integer code, which is why the firmware there looks
 * like it uses a plain linear value. The real formulas, for this board (revision "B":
 * DAC_RES = 12, V_REF = 2.5 -- BOARDS_SETTINGS in that same file, matching this AD5697):
 *
 *   fine   (__compute_fine_gain_dac):   code = gain * 2^12 / (2 * 2.5)        [LINEAR]
 *   coarse (__compute_coarse_gain_dac): code = 0.6 * 2^12 * log10(gain) / 2.5 [LOGARITHMIC]
 *
 * Valid gain ranges, also from that file (GAIN_FINE_LIMITS / GAIN_COARSE_LIMITS_B):
 *   fine   1.0 .. 2.0
 *   coarse 1.0 .. 21.0
 *
 * HISTORY -- why this is spelled out so carefully: this driver originally applied the COARSE
 * (logarithmic) formula to BOTH channels. Since log10(1.0) = 0, the default fine gain of 1.0
 * produced DAC code 0, i.e. channel A pinned at 0 V, the hard bottom of the AD8330's control
 * range, instead of its correct code of 819. (The coarse channel was fine: 6.0 -> 765.) So the
 * front end ran at a badly wrong operating point for a long stretch of bring-up, and every
 * threshold constant tuned during that period was meaningless.
 *
 * This was found while hunting the amplitude-dependent pulse distortion of bring-up, and was for a
 * while believed to be its cause -- overload recovery being a plausible reading of that shape. It
 * is NOT. A controlled bisect (firmware's test_vga_fine_bisect(), same bitstream, gain the only
 * variable) settled it: at fine code 0 the pulse amplitude is 41 counts with sigma 5, i.e. the
 * AD8330 output is essentially ZERO. VMAG at 0 V produces no signal, not a distorted one, so it
 * cannot explain traces full of large distorted pulses. The real cause was the ADC 2's-complement
 * misread -- see trigger_core_top.vhd, which reproduces it.
 *
 * That same bisect confirmed the control law empirically, which is worth recording: sigma tracked
 * the code proportionally (code 410 -> 29, 819 -> 58, 1638 -> 102), so the fine channel is LINEAR
 * and drives VMAG, matching the AD8330_VMAG port in docs/fpga/DPP4SiPMCMOD_AFE.pdf.
 */

#ifndef SRC_VGA_DAC_H_
#define SRC_VGA_DAC_H_

#include "xil_types.h"

/* Operating point. Fine matches the sibling's default (1.0). Coarse was 6.0 through early
 * bring-up (the sibling's main.py currently runs 6.4); changed to 2.0 on 2026-09-03 once the
 * full 1x-10x baseline-noise sweep (project log section 8k) gave the data to choose deliberately
 * rather than inherit the sibling's number: 2.0x puts this CLYC detector's ~6 MeVee ceiling at
 * 6.56 MeVee (headroom, not a hard cutoff -- 2.19x would land exactly on 6 MeVee, see the log for
 * why 2.0x was preferred), while the same sweep's SNR-vs-gain model shows SNR is still rising
 * steeply through this range (it only saturates past ~8x) -- so 6.0x traded away more than half
 * the dynamic range above 2.19x for well under one more unit of asymptotic SNR. */
#define AD8330_DEFAULT_GAIN_FINE_LINEAR 1.0
#define AD8330_DEFAULT_GAIN_COARSE_LINEAR 2.0

/**
 * @brief Enables the AD5697's internal 2.5V reference. Call once before any SetGain* call.
 * @return 1 on success, 0 on I2C failure.
 */
int VgaDac_Init(void);

/**
 * @brief Sets the AD8330's fine (linear, channel A / DAC A) gain.
 * @param gain_linear Clamped to the valid range for this channel, 1.0..2.0.
 * @return 1 on success, 0 on I2C failure.
 */
int VgaDac_SetGainFine(double gain_linear);

/**
 * @brief Sets the AD8330's coarse (logarithmic, channel B / DAC B) gain.
 * @param gain_linear Clamped to the valid range for this channel, 1.0..21.0.
 * @return 1 on success, 0 on I2C failure.
 */
int VgaDac_SetGainCoarse(double gain_linear);

/**
 * @brief DIAGNOSTIC ONLY -- writes a raw 12-bit code to the fine channel (DAC A), bypassing the
 *        gain formula and its range clamp. Exists so the bring-up firmware can reproduce the
 *        historical fine-gain-code-0 condition on demand and bisect it against the corrected code
 *        819; see the HISTORY note above. Not for normal operation -- use VgaDac_SetGainFine().
 * @param code12 Raw 12-bit DAC code, masked to 12 bits.
 * @return 1 on success, 0 on I2C failure.
 */
int VgaDac_SetFineCodeRaw(u16 code12);

#endif /* SRC_VGA_DAC_H_ */
