// ==============================================================
// Vitis HLS - High-Level Synthesis from C, C++ and OpenCL v2022.2 (64-bit)
// Tool Version Limit: 2019.12
// Copyright 1986-2022 Xilinx, Inc. All Rights Reserved.
// ==============================================================
#ifndef __linux__

#include "xstatus.h"
#include "xparameters.h"
#include "xfci_core.h"

extern XFci_core_Config XFci_core_ConfigTable[];

XFci_core_Config *XFci_core_LookupConfig(u16 DeviceId) {
	XFci_core_Config *ConfigPtr = NULL;

	int Index;

	for (Index = 0; Index < XPAR_XFCI_CORE_NUM_INSTANCES; Index++) {
		if (XFci_core_ConfigTable[Index].DeviceId == DeviceId) {
			ConfigPtr = &XFci_core_ConfigTable[Index];
			break;
		}
	}

	return ConfigPtr;
}

int XFci_core_Initialize(XFci_core *InstancePtr, u16 DeviceId) {
	XFci_core_Config *ConfigPtr;

	Xil_AssertNonvoid(InstancePtr != NULL);

	ConfigPtr = XFci_core_LookupConfig(DeviceId);
	if (ConfigPtr == NULL) {
		InstancePtr->IsReady = 0;
		return (XST_DEVICE_NOT_FOUND);
	}

	return XFci_core_CfgInitialize(InstancePtr, ConfigPtr);
}

#endif

