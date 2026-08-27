
wire kernel_monitor_reset;
wire kernel_monitor_clock;
wire kernel_monitor_report;
assign kernel_monitor_reset = ~ap_rst_n;
assign kernel_monitor_clock = ap_clk;
assign kernel_monitor_report = 1'b0;
wire [1:0] axis_block_sigs;
wire [7:0] inst_idle_sigs;
wire [3:0] inst_block_sigs;
wire kernel_block;

assign axis_block_sigs[0] = ~axis_to_fft_U0.grp_axis_to_fft_Pipeline_SAMPLE_LOOP_fu_71.s_axis_data_TDATA_blk_n;
assign axis_block_sigs[1] = ~fft_to_psa_U0.m_axis_result_TDATA_blk_n;

assign inst_idle_sigs[0] = entry_proc_U0.ap_idle;
assign inst_block_sigs[0] = (entry_proc_U0.ap_done & ~entry_proc_U0.ap_continue) | ~entry_proc_U0.psa_l_lo_c_blk_n | ~entry_proc_U0.psa_l_hi_c_blk_n | ~entry_proc_U0.psa_w_lo_c_blk_n | ~entry_proc_U0.psa_w_hi_c_blk_n;
assign inst_idle_sigs[1] = axis_to_fft_U0.ap_idle;
assign inst_block_sigs[1] = (axis_to_fft_U0.ap_done & ~axis_to_fft_U0.ap_continue) | ~axis_to_fft_U0.grp_axis_to_fft_Pipeline_SAMPLE_LOOP_fu_71.xn_s_blk_n | ~axis_to_fft_U0.config_s18_blk_n | ~axis_to_fft_U0.tag_s19_blk_n;
assign inst_idle_sigs[2] = fft_fci_fft_config_U0.ap_idle;
assign inst_block_sigs[2] = (fft_fci_fft_config_U0.ap_done & ~fft_fci_fft_config_U0.ap_continue);
assign inst_idle_sigs[3] = fft_to_psa_U0.ap_idle;
assign inst_block_sigs[3] = (fft_to_psa_U0.ap_done & ~fft_to_psa_U0.ap_continue) | ~fft_to_psa_U0.grp_fft_to_psa_Pipeline_BIN_LOOP_fu_194.xk_s_blk_n | ~fft_to_psa_U0.status_s17_blk_n | ~fft_to_psa_U0.psa_l_lo_blk_n | ~fft_to_psa_U0.psa_l_hi_blk_n | ~fft_to_psa_U0.psa_w_lo_blk_n | ~fft_to_psa_U0.psa_w_hi_blk_n | ~fft_to_psa_U0.tag_s19_blk_n;

assign inst_idle_sigs[4] = 1'b0;
assign inst_idle_sigs[5] = axis_to_fft_U0.ap_idle;
assign inst_idle_sigs[6] = axis_to_fft_U0.grp_axis_to_fft_Pipeline_SAMPLE_LOOP_fu_71.ap_idle;
assign inst_idle_sigs[7] = fft_to_psa_U0.ap_idle;

fci_core_hls_deadlock_idx0_monitor fci_core_hls_deadlock_idx0_monitor_U (
    .clock(kernel_monitor_clock),
    .reset(kernel_monitor_reset),
    .axis_block_sigs(axis_block_sigs),
    .inst_idle_sigs(inst_idle_sigs),
    .inst_block_sigs(inst_block_sigs),
    .block(kernel_block)
);


always @ (kernel_block or kernel_monitor_reset) begin
    if (kernel_block == 1'b1 && kernel_monitor_reset == 1'b0) begin
        find_kernel_block = 1'b1;
    end
    else begin
        find_kernel_block = 1'b0;
    end
end
