# Definitional proc to organize widgets for parameters.
proc init_gui { IPINST } {
  ipgui::add_param $IPINST -name "Component_Name"
  #Adding Page
  set Page_0 [ipgui::add_page $IPINST -name "Page 0"]
  ipgui::add_param $IPINST -name "ACC_WIDTH" -parent ${Page_0}
  ipgui::add_param $IPINST -name "DATA_WIDTH" -parent ${Page_0}
  ipgui::add_param $IPINST -name "DECAY_BITS" -parent ${Page_0}
  ipgui::add_param $IPINST -name "FIFO_DEPTH" -parent ${Page_0}
  ipgui::add_param $IPINST -name "K_MAX" -parent ${Page_0}
  ipgui::add_param $IPINST -name "M_MAX" -parent ${Page_0}
  ipgui::add_param $IPINST -name "RECIP_BITS" -parent ${Page_0}
  ipgui::add_param $IPINST -name "RECIP_FRAC_BITS" -parent ${Page_0}


}

proc update_PARAM_VALUE.ACC_WIDTH { PARAM_VALUE.ACC_WIDTH } {
	# Procedure called to update ACC_WIDTH when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.ACC_WIDTH { PARAM_VALUE.ACC_WIDTH } {
	# Procedure called to validate ACC_WIDTH
	return true
}

proc update_PARAM_VALUE.DATA_WIDTH { PARAM_VALUE.DATA_WIDTH } {
	# Procedure called to update DATA_WIDTH when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.DATA_WIDTH { PARAM_VALUE.DATA_WIDTH } {
	# Procedure called to validate DATA_WIDTH
	return true
}

proc update_PARAM_VALUE.DECAY_BITS { PARAM_VALUE.DECAY_BITS } {
	# Procedure called to update DECAY_BITS when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.DECAY_BITS { PARAM_VALUE.DECAY_BITS } {
	# Procedure called to validate DECAY_BITS
	return true
}

proc update_PARAM_VALUE.FIFO_DEPTH { PARAM_VALUE.FIFO_DEPTH } {
	# Procedure called to update FIFO_DEPTH when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.FIFO_DEPTH { PARAM_VALUE.FIFO_DEPTH } {
	# Procedure called to validate FIFO_DEPTH
	return true
}

proc update_PARAM_VALUE.K_MAX { PARAM_VALUE.K_MAX } {
	# Procedure called to update K_MAX when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.K_MAX { PARAM_VALUE.K_MAX } {
	# Procedure called to validate K_MAX
	return true
}

proc update_PARAM_VALUE.M_MAX { PARAM_VALUE.M_MAX } {
	# Procedure called to update M_MAX when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.M_MAX { PARAM_VALUE.M_MAX } {
	# Procedure called to validate M_MAX
	return true
}

proc update_PARAM_VALUE.RECIP_BITS { PARAM_VALUE.RECIP_BITS } {
	# Procedure called to update RECIP_BITS when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.RECIP_BITS { PARAM_VALUE.RECIP_BITS } {
	# Procedure called to validate RECIP_BITS
	return true
}

proc update_PARAM_VALUE.RECIP_FRAC_BITS { PARAM_VALUE.RECIP_FRAC_BITS } {
	# Procedure called to update RECIP_FRAC_BITS when any of the dependent parameters in the arguments change
}

proc validate_PARAM_VALUE.RECIP_FRAC_BITS { PARAM_VALUE.RECIP_FRAC_BITS } {
	# Procedure called to validate RECIP_FRAC_BITS
	return true
}


proc update_MODELPARAM_VALUE.DATA_WIDTH { MODELPARAM_VALUE.DATA_WIDTH PARAM_VALUE.DATA_WIDTH } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.DATA_WIDTH}] ${MODELPARAM_VALUE.DATA_WIDTH}
}

proc update_MODELPARAM_VALUE.ACC_WIDTH { MODELPARAM_VALUE.ACC_WIDTH PARAM_VALUE.ACC_WIDTH } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.ACC_WIDTH}] ${MODELPARAM_VALUE.ACC_WIDTH}
}

proc update_MODELPARAM_VALUE.FIFO_DEPTH { MODELPARAM_VALUE.FIFO_DEPTH PARAM_VALUE.FIFO_DEPTH } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.FIFO_DEPTH}] ${MODELPARAM_VALUE.FIFO_DEPTH}
}

proc update_MODELPARAM_VALUE.K_MAX { MODELPARAM_VALUE.K_MAX PARAM_VALUE.K_MAX } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.K_MAX}] ${MODELPARAM_VALUE.K_MAX}
}

proc update_MODELPARAM_VALUE.M_MAX { MODELPARAM_VALUE.M_MAX PARAM_VALUE.M_MAX } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.M_MAX}] ${MODELPARAM_VALUE.M_MAX}
}

proc update_MODELPARAM_VALUE.DECAY_BITS { MODELPARAM_VALUE.DECAY_BITS PARAM_VALUE.DECAY_BITS } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.DECAY_BITS}] ${MODELPARAM_VALUE.DECAY_BITS}
}

proc update_MODELPARAM_VALUE.RECIP_BITS { MODELPARAM_VALUE.RECIP_BITS PARAM_VALUE.RECIP_BITS } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.RECIP_BITS}] ${MODELPARAM_VALUE.RECIP_BITS}
}

proc update_MODELPARAM_VALUE.RECIP_FRAC_BITS { MODELPARAM_VALUE.RECIP_FRAC_BITS PARAM_VALUE.RECIP_FRAC_BITS } {
	# Procedure called to set VHDL generic/Verilog parameter value(s) based on TCL parameter value
	set_property value [get_property value ${PARAM_VALUE.RECIP_FRAC_BITS}] ${MODELPARAM_VALUE.RECIP_FRAC_BITS}
}

