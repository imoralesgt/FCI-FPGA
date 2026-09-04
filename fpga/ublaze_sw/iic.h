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

/**
 * @brief Initializes the axi_iic_0 driver instance for the given device ID.
 * @param DeviceId XPAR device ID for the target IIC controller (see xparameters.h).
 * @return XST_SUCCESS on success, XST_FAILURE if the device could not be looked up/configured.
 */
int Iic_Init(u16 DeviceId);

/** @brief Sets the 7-bit slave address used by the next Iic_DynamicSendBytes() call. */
void Iic_SetAddress(int Address);

/**
 * @brief Sends bytes as a single START..STOP dynamic-mode transaction, with retry.
 *
 * Writes @p byte_to_send bytes (<= 15) as a single START..STOP transaction to the address set by
 * Iic_SetAddress(). Retries internally on a transmission error.
 *
 * @param buffer_to_send Bytes to write.
 * @param byte_to_send   Number of bytes in @p buffer_to_send (<= 15).
 * @return 1 on success, 0 if all retries were exhausted.
 */
int Iic_DynamicSendBytes(u8 *buffer_to_send, int byte_to_send);

#endif /* SRC_IIC_H_ */
