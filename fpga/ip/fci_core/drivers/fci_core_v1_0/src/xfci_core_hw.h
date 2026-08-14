// ==============================================================
// Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2022.2 (64-bit)
// Tool Version Limit: 2019.12
// Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
// ==============================================================
// control
// 0x00 : Control signals
//        bit 0  - ap_start (Read/Write/COH)
//        bit 1  - ap_done (Read/COR)
//        bit 2  - ap_idle (Read)
//        bit 3  - ap_ready (Read/COR)
//        bit 7  - auto_restart (Read/Write)
//        bit 9  - interrupt (Read)
//        others - reserved
// 0x04 : Global Interrupt Enable Register
//        bit 0  - Global Interrupt Enable (Read/Write)
//        others - reserved
// 0x08 : IP Interrupt Enable Register (Read/Write)
//        bit 0 - enable ap_done interrupt (Read/Write)
//        bit 1 - enable ap_ready interrupt (Read/Write)
//        others - reserved
// 0x0c : IP Interrupt Status Register (Read/TOW)
//        bit 0 - ap_done (Read/TOW)
//        bit 1 - ap_ready (Read/TOW)
//        others - reserved
// 0x10 : Data signal of psa_l_lo
//        bit 9~0 - psa_l_lo[9:0] (Read/Write)
//        others  - reserved
// 0x14 : reserved
// 0x18 : Data signal of psa_l_hi
//        bit 9~0 - psa_l_hi[9:0] (Read/Write)
//        others  - reserved
// 0x1c : reserved
// 0x20 : Data signal of psa_w_lo
//        bit 9~0 - psa_w_lo[9:0] (Read/Write)
//        others  - reserved
// 0x24 : reserved
// 0x28 : Data signal of psa_w_hi
//        bit 9~0 - psa_w_hi[9:0] (Read/Write)
//        others  - reserved
// 0x2c : reserved
// (SC = Self Clear, COR = Clear on Read, TOW = Toggle on Write, COH = Clear on Handshake)

#define XFCI_CORE_CONTROL_ADDR_AP_CTRL       0x00
#define XFCI_CORE_CONTROL_ADDR_GIE           0x04
#define XFCI_CORE_CONTROL_ADDR_IER           0x08
#define XFCI_CORE_CONTROL_ADDR_ISR           0x0c
#define XFCI_CORE_CONTROL_ADDR_PSA_L_LO_DATA 0x10
#define XFCI_CORE_CONTROL_BITS_PSA_L_LO_DATA 10
#define XFCI_CORE_CONTROL_ADDR_PSA_L_HI_DATA 0x18
#define XFCI_CORE_CONTROL_BITS_PSA_L_HI_DATA 10
#define XFCI_CORE_CONTROL_ADDR_PSA_W_LO_DATA 0x20
#define XFCI_CORE_CONTROL_BITS_PSA_W_LO_DATA 10
#define XFCI_CORE_CONTROL_ADDR_PSA_W_HI_DATA 0x28
#define XFCI_CORE_CONTROL_BITS_PSA_W_HI_DATA 10

