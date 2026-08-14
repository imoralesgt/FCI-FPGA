// ==============================================================
// Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2022.2 (64-bit)
// Tool Version Limit: 2019.12
// Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
// ==============================================================
/***************************** Include Files *********************************/
#include "xfci_core.h"

/************************** Function Implementation *************************/
#ifndef __linux__
int XFci_core_CfgInitialize(XFci_core *InstancePtr, XFci_core_Config *ConfigPtr) {
    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(ConfigPtr != NULL);

    InstancePtr->Control_BaseAddress = ConfigPtr->Control_BaseAddress;
    InstancePtr->IsReady = XIL_COMPONENT_IS_READY;

    return XST_SUCCESS;
}
#endif

void XFci_core_Start(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_AP_CTRL) & 0x80;
    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_AP_CTRL, Data | 0x01);
}

u32 XFci_core_IsDone(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_AP_CTRL);
    return (Data >> 1) & 0x1;
}

u32 XFci_core_IsIdle(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_AP_CTRL);
    return (Data >> 2) & 0x1;
}

u32 XFci_core_IsReady(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_AP_CTRL);
    // check ap_start to see if the pcore is ready for next input
    return !(Data & 0x1);
}

void XFci_core_EnableAutoRestart(XFci_core *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_AP_CTRL, 0x80);
}

void XFci_core_DisableAutoRestart(XFci_core *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_AP_CTRL, 0);
}

void XFci_core_Set_psa_l_lo(XFci_core *InstancePtr, u32 Data) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_L_LO_DATA, Data);
}

u32 XFci_core_Get_psa_l_lo(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_L_LO_DATA);
    return Data;
}

void XFci_core_Set_psa_l_hi(XFci_core *InstancePtr, u32 Data) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_L_HI_DATA, Data);
}

u32 XFci_core_Get_psa_l_hi(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_L_HI_DATA);
    return Data;
}

void XFci_core_Set_psa_w_lo(XFci_core *InstancePtr, u32 Data) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_W_LO_DATA, Data);
}

u32 XFci_core_Get_psa_w_lo(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_W_LO_DATA);
    return Data;
}

void XFci_core_Set_psa_w_hi(XFci_core *InstancePtr, u32 Data) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_W_HI_DATA, Data);
}

u32 XFci_core_Get_psa_w_hi(XFci_core *InstancePtr) {
    u32 Data;

    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Data = XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_PSA_W_HI_DATA);
    return Data;
}

void XFci_core_InterruptGlobalEnable(XFci_core *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_GIE, 1);
}

void XFci_core_InterruptGlobalDisable(XFci_core *InstancePtr) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_GIE, 0);
}

void XFci_core_InterruptEnable(XFci_core *InstancePtr, u32 Mask) {
    u32 Register;

    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Register =  XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_IER);
    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_IER, Register | Mask);
}

void XFci_core_InterruptDisable(XFci_core *InstancePtr, u32 Mask) {
    u32 Register;

    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    Register =  XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_IER);
    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_IER, Register & (~Mask));
}

void XFci_core_InterruptClear(XFci_core *InstancePtr, u32 Mask) {
    Xil_AssertVoid(InstancePtr != NULL);
    Xil_AssertVoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    XFci_core_WriteReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_ISR, Mask);
}

u32 XFci_core_InterruptGetEnabled(XFci_core *InstancePtr) {
    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    return XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_IER);
}

u32 XFci_core_InterruptGetStatus(XFci_core *InstancePtr) {
    Xil_AssertNonvoid(InstancePtr != NULL);
    Xil_AssertNonvoid(InstancePtr->IsReady == XIL_COMPONENT_IS_READY);

    return XFci_core_ReadReg(InstancePtr->Control_BaseAddress, XFCI_CORE_CONTROL_ADDR_ISR);
}

