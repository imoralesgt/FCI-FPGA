/*
 * vga_dac.h
 *
 * AD5697 dual 12-bit DAC (I2C addr 0x0D) driving the AD8330 VGA's fine (channel A) and coarse
 * (channel B) gain-control voltages, per docs/fpga/DPP4SiPMCMOD_AFE.pdf. Uses the AD5697's
 * internal 2.5V reference (confirmed for this board), matching the sibling project's current
 * hardware default.
 *
 * DAC code vs. linear gain follows this board's calibrated log relationship:
 *   code = 0.6 * 4096 / VREF * log10(gain_linear)
 * (gain_linear >= 1; the AD8330's response is dB-linear in VMAG, and VMAG is linear in code).
 */

#ifndef SRC_VGA_DAC_H_
#define SRC_VGA_DAC_H_

#include "xil_types.h"

/* Confirmed default operating point. */
#define AD8330_DEFAULT_GAIN_FINE_LINEAR 1.0
#define AD8330_DEFAULT_GAIN_COARSE_LINEAR 6.0

/* Enables the AD5697's internal 2.5V reference. Call once before any SetGain* call. Returns 1 on
 * success, 0 on I2C failure. */
int VgaDac_Init(void);

/* gain_linear >= 1.0; out-of-range/out-of-DAC-span values are clamped. Returns 1 on success, 0 on
 * I2C failure. */
int VgaDac_SetGainFine(double gain_linear);
int VgaDac_SetGainCoarse(double gain_linear);

#endif /* SRC_VGA_DAC_H_ */
