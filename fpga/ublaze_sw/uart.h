/*
 * uart.h
 *
 * The CLI's receive path, abstracted over which UART the block design actually contains.
 *
 * Two are supported. axi_uartlite was the original and caps at 921600 baud -- a fixed choice list
 * in the IP, not a computed range, so it cannot be pushed higher. axi_uart16550 replaces it because
 * its baud comes from a RUNTIME divisor latch over an external reference clock, which is what
 * lifted the readout ceiling: at 64 MHz on `xin`, divisor 1 gives exactly 4 Mbaud and divisor 2
 * gives exactly 2 Mbaud, both of which the FT2232H also generates exactly (12 MHz / n), so neither
 * end accumulates bit error.
 *
 * Selected from the XPAR symbols the BSP exports rather than from a hand-set switch. That rule is
 * not stylistic: this firmware has already been broken once by a compile-time switch that keyed on
 * what the old hardware was CALLED (XPAR_FCI_SINK_0_BASEADDR) rather than on what the new hardware
 * HAS, and silently selected a dead code path when the block design changed under it. Deriving
 * from the peripheral's own presence cannot drift from the bitstream.
 *
 * Only the RX path needs this. Transmission goes through xil_printf()/outbyte(), which the BSP
 * routes to whichever peripheral is configured as stdout -- so it follows the hardware
 * automatically, with no code here.
 */

#ifndef SRC_UART_H_
#define SRC_UART_H_

#include "xparameters.h"
#include "xstatus.h"

#if defined(XPAR_UARTNS550_0_BASEADDR) || defined(XPAR_AXI_UART16550_0_BASEADDR)
#define UART_IS_16550 1
#else
#define UART_IS_16550 0
#endif

#if UART_IS_16550
#include "xuartns550_l.h"
#if defined(XPAR_UARTNS550_0_BASEADDR)
#define UART_BASEADDR XPAR_UARTNS550_0_BASEADDR
#else
#define UART_BASEADDR XPAR_AXI_UART16550_0_BASEADDR
#endif
#else
#include "xuartlite_l.h"
#define UART_BASEADDR XPAR_UARTLITE_0_BASEADDR
#endif

/* Frequency on the 16550's `xin` pin, which is what its divisor latch actually divides -- NOT the
 * AXI clock. clk_wiz_1 emits 64 MHz for this (12 MHz sys_clock x 64 / 12, the only exact solution
 * from this board's oscillator, and below the PLL VCO floor so it must come from an MMCM).
 *
 * Stated here rather than read from XPAR because the BSP's own clock symbol has been observed to
 * report the AXI frequency for this core; a wrong value here silently produces a wrong baud rate,
 * which looks like line noise rather than a configuration error. If the block design's clk_wiz_1
 * output changes, this must change with it. */
#define UART_XIN_HZ 64000000u

/* Divisor 1 -> 4 Mbaud, divisor 2 -> 2 Mbaud. Both exact on this XIN, and both exactly generated
 * by the FT2232H, so there is no cumulative sampling error at either end. Running at 4 Mbaud
 * (divisor 1); dropping to 2 Mbaud if the link proves marginal is a one-character change here. */
#define UART_BAUD_DIVISOR 1u
#define UART_BAUD_HZ (UART_XIN_HZ / (16u * UART_BAUD_DIVISOR))

/* Programs the baud divisor. No-op on uartlite, whose rate is fixed at synthesis. Call once during
 * bring-up, BEFORE anything is printed: it reconfigures the line, so any character in flight is
 * corrupted, and the host must already be at the matching rate. */
void Uart_Init(void);

/* Non-zero when at least one received byte is waiting. */
int Uart_HasByte(void);

/* Reads one byte. Only valid when Uart_HasByte() is non-zero. */
char Uart_GetByte(void);

#endif /* SRC_UART_H_ */
