/*
 * uart.c
 *
 * See uart.h.
 */

#include "uart.h"

#include "xil_io.h"

#if UART_IS_16550

/** @brief See uart.h (16550 build: programs the divisor latch, 8N1, and enables both FIFOs). */
void Uart_Init(void) {
  u32 lcr;

  /* The divisor latch shares register addresses with the receive/transmit and interrupt-enable
   * registers; DLAB in the line control register selects which set is visible. So: raise DLAB,
   * write the divisor, lower DLAB again. Leaving DLAB set would make every later access to the
   * data register land on the divisor instead -- the UART would appear completely dead. */
  lcr = XUartNs550_ReadReg(UART_BASEADDR, XUN_LCR_OFFSET);
  XUartNs550_WriteReg(UART_BASEADDR, XUN_LCR_OFFSET, lcr | XUN_LCR_DLAB);

  XUartNs550_WriteReg(UART_BASEADDR, XUN_DLL_OFFSET, UART_BAUD_DIVISOR & 0xFFu);
  XUartNs550_WriteReg(UART_BASEADDR, XUN_DLM_OFFSET, (UART_BAUD_DIVISOR >> 8) & 0xFFu);

  /* 8N1, and DLAB cleared in the same write. */
  XUartNs550_WriteReg(UART_BASEADDR, XUN_LCR_OFFSET, XUN_LCR_8_DATA_BITS);

  /* Enable the FIFOs. NOT optional, and its absence is not a performance issue -- it is a
   * correctness one.
   *
   * With the FIFOs disabled a 16550 receives into a SINGLE byte holding register, so any character
   * arriving before firmware reads the previous one is lost to an overrun. This CLI polls the UART
   * from the main loop, and while it is streaming a binary $RQ frame (25.6 kB, ~64 ms at 4 Mbaud)
   * it does not poll at all. The host, being synchronous, sends the next 9-byte command as soon as
   * it finishes reading -- straight into that window. Eight of those nine bytes were overwritten,
   * firmware parsed the fragment, and answered !XX 0 / !XX 1.
   *
   * Measured before this fix: 37 rejected commands in a 240 s soak (1.1% of polls, ~2.9M events).
   * The 16-byte RX FIFO covers the longest command this protocol defines with room to spare.
   * Both FIFOs are reset here so nothing left over from before the baud change is delivered as
   * data at the new rate. */
  XUartNs550_WriteReg(UART_BASEADDR, XUN_FCR_OFFSET,
                      XUN_FIFO_ENABLE | XUN_FIFO_RX_RESET | XUN_FIFO_TX_RESET);
}

/** @brief See uart.h (16550 build). */
int Uart_HasByte(void) { return XUartNs550_IsReceiveData(UART_BASEADDR) ? 1 : 0; }

/** @brief See uart.h (16550 build). */
char Uart_GetByte(void) {
  return (char)XUartNs550_ReadReg(UART_BASEADDR, XUN_RBR_OFFSET);
}

#else /* axi_uartlite: baud is fixed at synthesis, nothing to program */

/** @brief See uart.h (uartlite build: no-op, baud is fixed at synthesis). */
void Uart_Init(void) {}

/** @brief See uart.h (uartlite build). */
int Uart_HasByte(void) { return !XUartLite_IsReceiveEmpty(UART_BASEADDR); }

/** @brief See uart.h (uartlite build). */
char Uart_GetByte(void) { return (char)XUartLite_RecvByte(UART_BASEADDR); }

#endif
