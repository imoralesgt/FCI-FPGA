// ==============================================================
// Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2022.2 (64-bit)
// Tool Version Limit: 2019.12
// Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
// ==============================================================
#ifndef XFCI_CORE_H
#define XFCI_CORE_H

#ifdef __cplusplus
extern "C" {
#endif

/***************************** Include Files *********************************/
#ifndef __linux__
#include "xil_types.h"
#include "xil_assert.h"
#include "xstatus.h"
#include "xil_io.h"
#else
#include <stdint.h>
#include <assert.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stddef.h>
#endif
#include "xfci_core_hw.h"

/**************************** Type Definitions ******************************/
#ifdef __linux__
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#else
typedef struct {
    u16 DeviceId;
    u64 Control_BaseAddress;
} XFci_core_Config;
#endif

typedef struct {
    u64 Control_BaseAddress;
    u32 IsReady;
} XFci_core;

typedef u32 word_type;

/***************** Macros (Inline Functions) Definitions *********************/
#ifndef __linux__
#define XFci_core_WriteReg(BaseAddress, RegOffset, Data) \
    Xil_Out32((BaseAddress) + (RegOffset), (u32)(Data))
#define XFci_core_ReadReg(BaseAddress, RegOffset) \
    Xil_In32((BaseAddress) + (RegOffset))
#else
#define XFci_core_WriteReg(BaseAddress, RegOffset, Data) \
    *(volatile u32*)((BaseAddress) + (RegOffset)) = (u32)(Data)
#define XFci_core_ReadReg(BaseAddress, RegOffset) \
    *(volatile u32*)((BaseAddress) + (RegOffset))

#define Xil_AssertVoid(expr)    assert(expr)
#define Xil_AssertNonvoid(expr) assert(expr)

#define XST_SUCCESS             0
#define XST_DEVICE_NOT_FOUND    2
#define XST_OPEN_DEVICE_FAILED  3
#define XIL_COMPONENT_IS_READY  1
#endif

/************************** Function Prototypes *****************************/
#ifndef __linux__
int XFci_core_Initialize(XFci_core *InstancePtr, u16 DeviceId);
XFci_core_Config* XFci_core_LookupConfig(u16 DeviceId);
int XFci_core_CfgInitialize(XFci_core *InstancePtr, XFci_core_Config *ConfigPtr);
#else
int XFci_core_Initialize(XFci_core *InstancePtr, const char* InstanceName);
int XFci_core_Release(XFci_core *InstancePtr);
#endif

void XFci_core_Start(XFci_core *InstancePtr);
u32 XFci_core_IsDone(XFci_core *InstancePtr);
u32 XFci_core_IsIdle(XFci_core *InstancePtr);
u32 XFci_core_IsReady(XFci_core *InstancePtr);
void XFci_core_EnableAutoRestart(XFci_core *InstancePtr);
void XFci_core_DisableAutoRestart(XFci_core *InstancePtr);

void XFci_core_Set_psa_l_lo(XFci_core *InstancePtr, u32 Data);
u32 XFci_core_Get_psa_l_lo(XFci_core *InstancePtr);
void XFci_core_Set_psa_l_hi(XFci_core *InstancePtr, u32 Data);
u32 XFci_core_Get_psa_l_hi(XFci_core *InstancePtr);
void XFci_core_Set_psa_w_lo(XFci_core *InstancePtr, u32 Data);
u32 XFci_core_Get_psa_w_lo(XFci_core *InstancePtr);
void XFci_core_Set_psa_w_hi(XFci_core *InstancePtr, u32 Data);
u32 XFci_core_Get_psa_w_hi(XFci_core *InstancePtr);

void XFci_core_InterruptGlobalEnable(XFci_core *InstancePtr);
void XFci_core_InterruptGlobalDisable(XFci_core *InstancePtr);
void XFci_core_InterruptEnable(XFci_core *InstancePtr, u32 Mask);
void XFci_core_InterruptDisable(XFci_core *InstancePtr, u32 Mask);
void XFci_core_InterruptClear(XFci_core *InstancePtr, u32 Mask);
u32 XFci_core_InterruptGetEnabled(XFci_core *InstancePtr);
u32 XFci_core_InterruptGetStatus(XFci_core *InstancePtr);

#ifdef __cplusplus
}
#endif

#endif
