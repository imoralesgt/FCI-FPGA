/*
 * vga_dac.c
 *
 * See vga_dac.h -- in particular the note that the fine and coarse channels use different
 * control laws (linear vs. logarithmic), ported from the sibling project's host-side Python API.
 *
 * Command byte encoding (0x70 = internal reference setup, 0x31/0x38 = write-to-and-update DAC
 * A/B) and the 12-bit-left-justified data framing are ported from the sibling project's
 * WriteCommandToAD5697(), and verified byte-for-byte against it.
 */

#include "vga_dac.h"

#include <math.h>

#include "iic.h"

#define AD5697_I2C_ADDR 0x0D
#define AD5697_VREF_VOLTS 2.5
#define AD5697_CODES 4096.0 /* 2^12; board revision "B" -- see vga_dac.h */

/* Valid gain ranges (sibling: GAIN_FINE_LIMITS, GAIN_COARSE_LIMITS_B). */
#define GAIN_FINE_MIN 1.0
#define GAIN_FINE_MAX 2.0
#define GAIN_COARSE_MIN 1.0
#define GAIN_COARSE_MAX 21.0

/** @brief Clamps v to [lo, hi]. */
static double clamp(double v, double lo, double hi) {
  if (v < lo)
    return lo;
  if (v > hi)
    return hi;
  return v;
}

/** @brief Clamps and truncates a computed DAC code to the AD5697's valid 12-bit range 0..4095. */
static u16 clamp_code(double code) {
  if (code < 0.0)
    return 0;
  if (code > 4095.0)
    return 4095;
  return (u16)code;
}

/**
 * @brief Sends one AD5697 command frame: command byte, then the 12-bit data left-justified into
 *        the following two bytes' top nibbles, per the AD5697's datasheet framing.
 * @param command_byte AD5697 command (e.g. 0x70 reference setup, 0x31/0x38 write-and-update DAC
 *                      A/B).
 * @param data12       12-bit payload (reference config bits, or a DAC code).
 * @return 1 on success, 0 on I2C failure (see Iic_DynamicSendBytes()).
 */
static int WriteCommand(u8 command_byte, u16 data12) {
  u8 u[3];
  u[0] = command_byte;
  u[1] = (u8)((data12 & 0x0FF0) >> 4); /* D11..D4 */
  u[2] = (u8)((data12 & 0x000F) << 4); /* D3..D0, left-justified into the top nibble */

  Iic_SetAddress(AD5697_I2C_ADDR);
  return Iic_DynamicSendBytes(u, 3);
}

/** @brief LINEAR control law: code = gain * 2^res / (2 * VREF). gain 1.0 -> 819. */
static u16 FineGainToCode12(double gain_linear) {
  gain_linear = clamp(gain_linear, GAIN_FINE_MIN, GAIN_FINE_MAX);
  return clamp_code(gain_linear * AD5697_CODES / (2.0 * AD5697_VREF_VOLTS) + 0.5);
}

/** @brief LOGARITHMIC control law: code = 0.6 * 2^res * log10(gain) / VREF. gain 6.0 -> 765. */
static u16 CoarseGainToCode12(double gain_linear) {
  gain_linear = clamp(gain_linear, GAIN_COARSE_MIN, GAIN_COARSE_MAX);
  return clamp_code(0.6 * AD5697_CODES * log10(gain_linear) / AD5697_VREF_VOLTS + 0.5);
}

/** @brief See vga_dac.h. Sends the internal-reference-enable command (data bits are don't-care
 *         except D0, 0 = internal reference enabled). */
int VgaDac_Init(void) {
  return WriteCommand(0x70, 0x0000);
}

/** @brief See vga_dac.h. Write-to-and-update DAC A. */
int VgaDac_SetGainFine(double gain_linear) {
  return WriteCommand(0x31, FineGainToCode12(gain_linear));
}

/** @brief See vga_dac.h. Write-to-and-update DAC B. */
int VgaDac_SetGainCoarse(double gain_linear) {
  return WriteCommand(0x38, CoarseGainToCode12(gain_linear));
}

int VgaDac_SetFineCodeRaw(u16 code12) {
  return WriteCommand(0x31, (u16)(code12 & 0x0FFF)); /* write to and update DAC A */
}
