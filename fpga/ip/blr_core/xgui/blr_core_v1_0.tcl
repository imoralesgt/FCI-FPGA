# Definitional proc to organize widgets for parameters.
proc init_gui { IPINST } {
  ipgui::add_param $IPINST -name "Component_Name"
  #Adding Page
  set Page_0 [ipgui::add_page $IPINST -name "Page 0"]
  ipgui::add_param $IPINST -name "ADC_IS_2C" -parent ${Page_0}
  ipgui::add_param $IPINST -name "ADC_WIDTH" -parent ${Page_0}
  ipgui::add_param $IPINST -name "MAX_SHIFT" -parent ${Page_0}


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

proc update_PARAM_VALUE.MAX_SHIFT { PARAM_VALUE.MAX_SHIFT } {
	# Procedure called to update MAX_SHIFT when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.MAX_SHIFT { PARAM_VALUE.MAX_SHIFT } {
	# Procedure called to validate MAX_SHIFT
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

proc update_MODELPARAM_VALUE.MAX_SHIFT { MODELPARAM_VALUE.MAX_SHIFT PARAM_VALUE.MAX_SHIFT } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.MAX_SHIFT}] ${MODELPARAM_VALUE.MAX_SHIFT}
}

