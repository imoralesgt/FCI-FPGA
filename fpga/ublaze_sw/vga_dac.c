/*
 * vga_dac.c
 *
 * See vga_dac.h. Command byte encoding (0x70 = internal reference setup, 0x31/0x38 = write-to-
 * and-update DAC A/B) and the 12-bit-left-justified data framing are ported from the sibling
 * project's WriteCommandToAD5697(), already proven against this same DAC part.
 */

#include "vga_dac.h"

#include <math.h>

#include "iic.h"

#define AD5697_I2C_ADDR 0x0D
#define AD5697_VREF_VOLTS 2.5
#define GAIN_CODE_SCALE (0.6 * 4096.0 / AD5697_VREF_VOLTS) /* see vga_dac.h for the calibration */

static int WriteCommand(u8 command_byte, u16 data12) {
  u8 u[3];
  u[0] = command_byte;
  u[1] = (u8)((data12 & 0x0FF0) >> 4); /* D11..D4 */
  u[2] = (u8)((data12 & 0x000F) << 4); /* D3..D0, left-justified into the top nibble */

  Iic_SetAddress(AD5697_I2C_ADDR);
  return Iic_DynamicSendBytes(u, 3);
}

static u16 GainToCode12(double gain_linear) {
  if (gain_linear < 1.0) /* log10 < 0 has no representable (negative) code */
    gain_linear = 1.0;

  int code = (int)(GAIN_CODE_SCALE * log10(gain_linear) + 0.5);
  if (code > 4095)
    code = 4095;
  if (code < 0)
    code = 0;
  return (u16)code;
}

int VgaDac_Init(void) {
  /* Internal reference setup command: data bytes are don't-care except D0 (0 = internal
   * reference enabled). */
  return WriteCommand(0x70, 0x0000);
}

int VgaDac_SetGainFine(double gain_linear) {
  return WriteCommand(0x31, GainToCode12(gain_linear)); /* write to and update DAC A */
}

int VgaDac_SetGainCoarse(double gain_linear) {
  return WriteCommand(0x38, GainToCode12(gain_linear)); /* write to and update DAC B */
}
