/*
 * uart.c
 *
 * See uart.h.
 */

#include "uart.h"

#include "xil_io.h"

#if UART_IS_16550

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
}

int Uart_HasByte(void) { return XUartNs550_IsReceiveData(UART_BASEADDR) ? 1 : 0; }

char Uart_GetByte(void) {
  return (char)XUartNs550_ReadReg(UART_BASEADDR, XUN_RBR_OFFSET);
}

#else /* axi_uartlite: baud is fixed at synthesis, nothing to program */

void Uart_Init(void) {}

int Uart_HasByte(void) { return !XUartLite_IsReceiveEmpty(UART_BASEADDR); }

char Uart_GetByte(void) { return (char)XUartLite_RecvByte(UART_BASEADDR); }

#endif
