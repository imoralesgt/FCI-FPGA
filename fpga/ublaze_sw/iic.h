/*
 * iic.h
 *
 * Minimal AXI IIC dynamic-mode driver: write-only master transactions, no interrupts. Ported
 * from the sibling NSIL-MCA-DPP4SiPM project's iic.c (same axi_iic_0 core, same board), trimmed
 * to the send path only -- this project's current I2C use (AD5697 VGA gain DAC, see vga_dac.c)
 * is write-only.
 */

#ifndef SRC_IIC_H_
#define SRC_IIC_H_

#include "xil_types.h"

int Iic_Init(u16 DeviceId);
void Iic_SetAddress(int Address);

/* Writes byte_to_send bytes (<= 15) as a single START..STOP transaction to the address set by
 * Iic_SetAddress(). Retries internally on a transmission error. Returns 1 on success, 0 if all
 * retries were exhausted. */
int Iic_DynamicSendBytes(u8 *buffer_to_send, int byte_to_send);

#endif /* SRC_IIC_H_ */
