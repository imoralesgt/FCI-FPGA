/*
 * iic.c
 *
 * See iic.h. Bus-level sequencing (FIFO reset order, bus-busy check, dynamic START/STOP framing,
 * TX-error retry) is ported as-is from the sibling project's proven implementation against the
 * same axi_iic_0 core -- see AXI IIC Bus Interface v2.0 LogiCORE IP Product Guide (PG090), p.37.
 */

#include "iic.h"

#include "sleep.h"
#include "xiic.h"
#include "xiic_l.h"
#include "xil_io.h"

static XIic Iic;

/** @brief See iic.h. */
int Iic_Init(u16 DeviceId) {
  XIic_Config *ConfigPtr = XIic_LookupConfig(DeviceId);
  if (ConfigPtr == NULL)
    return XST_FAILURE;

  return XIic_CfgInitialize(&Iic, ConfigPtr, ConfigPtr->BaseAddress);
}

/** @brief See iic.h. */
void Iic_SetAddress(int Address) { XIic_SetAddress(&Iic, XII_ADDR_TO_SEND_TYPE, Address); }

/** @brief Acks a pending RX-FIFO-full interrupt status bit, if set (write-only use never reads
 *         data, but a stale RX_FULL flag from a prior state can otherwise block re-arming). */
static void ClearReceiveFifoFull(void) {
  u32 IIC_BASEADDR = Iic.BaseAddress;
  u32 isr_reg = Xil_In32(IIC_BASEADDR + XIIC_IISR_OFFSET);
  if (isr_reg & XIIC_INTR_RX_FULL_MASK)
    Xil_Out32(IIC_BASEADDR + XIIC_IISR_OFFSET, XIIC_INTR_RX_FULL_MASK);
}

/** @brief Resets the core's TX FIFO and RX path, waits out any bus-busy condition, then puts the
 *         controller into transmit-direction mode. Run once at the start of every transaction
 *         (see Iic_DynamicSendBytes()) so each attempt starts from a known-clean state regardless
 *         of how the previous one ended. */
static void PrepareBusInTransmitMode(void) {
  u32 IIC_BASEADDR = Iic.BaseAddress;
  u32 sr_reg, cr_reg;

  Xil_Out32(IIC_BASEADDR + XIIC_CR_REG_OFFSET, 0);
  usleep(10);
  Xil_Out32(IIC_BASEADDR + XIIC_CR_REG_OFFSET, XIIC_CR_ENABLE_DEVICE_MASK);
  usleep(10);
  Xil_Out32(IIC_BASEADDR + XIIC_CR_REG_OFFSET,
            XIIC_CR_ENABLE_DEVICE_MASK | XIIC_CR_TX_FIFO_RESET_MASK);
  usleep(10);
  Xil_Out32(IIC_BASEADDR + XIIC_CR_REG_OFFSET, 0);
  usleep(10);

  for (int i = 0; i < 16; i++)
    Xil_In32(IIC_BASEADDR + XIIC_DRR_REG_OFFSET);
  usleep(10);

  ClearReceiveFifoFull();
  usleep(10);

  Xil_Out32(IIC_BASEADDR + XIIC_CR_REG_OFFSET, 0);
  usleep(10);

  sr_reg = Xil_In32(IIC_BASEADDR + XIIC_SR_REG_OFFSET);
  if ((sr_reg & XIIC_SR_BUS_BUSY_MASK) == XIIC_SR_BUS_BUSY_MASK)
    usleep(1000);

  cr_reg = XIIC_CR_ENABLE_DEVICE_MASK | XIIC_CR_DIR_IS_TX_MASK;
  Xil_Out32(IIC_BASEADDR + XIIC_CR_REG_OFFSET, cr_reg);
}

/** @brief Queues the slave address byte with the dynamic-mode START bit set and the write
 *         direction bit, so the core issues a START condition and addresses the slave for a
 *         write as soon as this byte reaches the TX FIFO. */
static void SendAddressInTransmitMode(void) {
  u32 IIC_BASEADDR = Iic.BaseAddress;
  u32 daddr = XIIC_TX_DYN_START_MASK | XIIC_WRITE_OPERATION | ((Iic.AddrOfSlave << 1) & 0xFE);
  Xil_Out32(IIC_BASEADDR + XIIC_DTR_REG_OFFSET, daddr);
}

/** @brief Queues the payload bytes, setting the dynamic-mode STOP bit on the last one so the core
 *         issues a STOP condition immediately after it, ending the transaction. */
static void SendData(u8 *buffer_to_send, int byte_to_send) {
  u32 IIC_BASEADDR = Iic.BaseAddress;
  int imax = byte_to_send - 1;

  for (int i = 0; i <= imax; i++) {
    u32 wdata = buffer_to_send[i] & 0xFF;
    if (i == imax)
      wdata |= XIIC_TX_DYN_STOP_MASK;
    Xil_Out32(IIC_BASEADDR + XIIC_DTR_REG_OFFSET, wdata);
  }
}

/**
 * @brief Checks whether the just-queued transaction completed cleanly, resetting the core if not.
 *
 * A set TX_ERROR bit (e.g. a NACK) resets the control and status registers and reports failure
 * immediately. Otherwise, if the TX FIFO has not yet drained, waits @p wait_time once more and
 * rechecks -- the caller has already waited out the transaction's own nominal duration, so this
 * covers only the margin, not the whole transfer.
 *
 * @param wait_time Microseconds to wait for the TX FIFO to drain if it has not already.
 * @return 1 if the TX FIFO is empty (transaction sent) and no TX error occurred, 0 otherwise.
 */
static int SendSuccessfullyCompleted(u32 wait_time) {
  u32 IIC_BASEADDR = Iic.BaseAddress;
  u32 sr_reg = Xil_In32(IIC_BASEADDR + XIIC_SR_REG_OFFSET);
  u32 isr_reg = Xil_In32(IIC_BASEADDR + XIIC_IISR_OFFSET);

  if ((isr_reg & XIIC_INTR_TX_ERROR_MASK) == XIIC_INTR_TX_ERROR_MASK) {
    Xil_Out32(IIC_BASEADDR + XIIC_CR_REG_OFFSET, 0);
    Xil_Out32(IIC_BASEADDR + XIIC_RESETR_OFFSET, 0xA);
    return 0;
  }

  if ((sr_reg & XIIC_SR_TX_FIFO_EMPTY_MASK) == 0) {
    usleep(wait_time);
    sr_reg = Xil_In32(IIC_BASEADDR + XIIC_SR_REG_OFFSET);
    return (sr_reg & XIIC_SR_TX_FIFO_EMPTY_MASK) != 0;
  }
  return 1;
}

/** @brief See iic.h. */
int Iic_DynamicSendBytes(u8 *buffer_to_send, int byte_to_send) {
  for (int trials = 3; trials > 0; trials--) {
    PrepareBusInTransmitMode();
    SendAddressInTransmitMode();
    if (byte_to_send != 0)
      SendData(buffer_to_send, byte_to_send);

    /* T = 10us * 9 * (N+1); 9th bit is the ACK, 10us is one I2C clock period. */
    u32 wait_time = (byte_to_send + 1) * 90 + 1000;
    usleep(wait_time);

    if (SendSuccessfullyCompleted(wait_time))
      return 1;
  }
  return 0;
}
