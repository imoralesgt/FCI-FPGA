# Definitional proc to organize widgets for parameters.
proc init_gui { IPINST } {
  ipgui::add_param $IPINST -name "Component_Name"
  #Adding Page
  set Page_0 [ipgui::add_page $IPINST -name "Page 0"]
  ipgui::add_param $IPINST -name "ADC_IS_2C" -parent ${Page_0}
  ipgui::add_param $IPINST -name "ADC_WIDTH" -parent ${Page_0}
  ipgui::add_param $IPINST -name "CFD_ARM_WINDOW" -parent ${Page_0}
  ipgui::add_param $IPINST -name "CFD_DLY_MAX" -parent ${Page_0}
  ipgui::add_param $IPINST -name "CFD_FRAC_BITS" -parent ${Page_0}
  ipgui::add_param $IPINST -name "MAX_DELAY" -parent ${Page_0}
  ipgui::add_param $IPINST -name "MAX_DEPTH" -parent ${Page_0}


}

proc update_PARAM_VALUE.ADC_IS_2C { PARAM_VALUE.ADC_IS_2C } {
	# Procedure called to update ADC_IS_2C when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.ADC_IS_2C { PARAM_VALUE.ADC_IS_2C } {
	# Procedure called to validate ADC_IS_2C
	return true
}

proc update_PARAM_VALUE.ADC_WIDTH { PARAM_VALUE.ADC_WIDTH } {
	# Procedure called to update ADC_WIDTH when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.ADC_WIDTH { PARAM_VALUE.ADC_WIDTH } {
	# Procedure called to validate ADC_WIDTH
	return true
}

proc update_PARAM_VALUE.CFD_ARM_WINDOW { PARAM_VALUE.CFD_ARM_WINDOW } {
	# Procedure called to update CFD_ARM_WINDOW when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.CFD_ARM_WINDOW { PARAM_VALUE.CFD_ARM_WINDOW } {
	# Procedure called to validate CFD_ARM_WINDOW
	return true
}

proc update_PARAM_VALUE.CFD_DLY_MAX { PARAM_VALUE.CFD_DLY_MAX } {
	# Procedure called to update CFD_DLY_MAX when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.CFD_DLY_MAX { PARAM_VALUE.CFD_DLY_MAX } {
	# Procedure called to validate CFD_DLY_MAX
	return true
}

proc update_PARAM_VALUE.CFD_FRAC_BITS { PARAM_VALUE.CFD_FRAC_BITS } {
	# Procedure called to update CFD_FRAC_BITS when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.CFD_FRAC_BITS { PARAM_VALUE.CFD_FRAC_BITS } {
	# Procedure called to validate CFD_FRAC_BITS
	return true
}

proc update_PARAM_VALUE.MAX_DELAY { PARAM_VALUE.MAX_DELAY } {
	# Procedure called to update MAX_DELAY when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.MAX_DELAY { PARAM_VALUE.MAX_DELAY } {
	# Procedure called to validate MAX_DELAY
	return true
}

proc update_PARAM_VALUE.MAX_DEPTH { PARAM_VALUE.MAX_DEPTH } {
	# Procedure called to update MAX_DEPTH when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.MAX_DEPTH { PARAM_VALUE.MAX_DEPTH } {
	# Procedure called to validate MAX_DEPTH
	return true
}


proc update_MODELPARAM_VALUE.ADC_WIDTH { MODELPARAM_VALUE.ADC_WIDTH PARAM_VALUE.ADC_WIDTH } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.ADC_WIDTH}] ${MODELPARAM_VALUE.ADC_WIDTH}
}

proc update_MODELPARAM_VALUE.ADC_IS_2C { MODELPARAM_VALUE.ADC_IS_2C PARAM_VALUE.ADC_IS_2C } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.ADC_IS_2C}] ${MODELPARAM_VALUE.ADC_IS_2C}
}

proc update_MODELPARAM_VALUE.MAX_DELAY { MODELPARAM_VALUE.MAX_DELAY PARAM_VALUE.MAX_DELAY } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.MAX_DELAY}] ${MODELPARAM_VALUE.MAX_DELAY}
}

proc update_MODELPARAM_VALUE.MAX_DEPTH { MODELPARAM_VALUE.MAX_DEPTH PARAM_VALUE.MAX_DEPTH } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.MAX_DEPTH}] ${MODELPARAM_VALUE.MAX_DEPTH}
}

proc update_MODELPARAM_VALUE.CFD_DLY_MAX { MODELPARAM_VALUE.CFD_DLY_MAX PARAM_VALUE.CFD_DLY_MAX } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.CFD_DLY_MAX}] ${MODELPARAM_VALUE.CFD_DLY_MAX}
}

proc update_MODELPARAM_VALUE.CFD_FRAC_BITS { MODELPARAM_VALUE.CFD_FRAC_BITS PARAM_VALUE.CFD_FRAC_BITS } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.CFD_FRAC_BITS}] ${MODELPARAM_VALUE.CFD_FRAC_BITS}
}

proc update_MODELPARAM_VALUE.CFD_ARM_WINDOW { MODELPARAM_VALUE.CFD_ARM_WINDOW PARAM_VALUE.CFD_ARM_WINDOW } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.CFD_ARM_WINDOW}] ${MODELPARAM_VALUE.CFD_ARM_WINDOW}
}

